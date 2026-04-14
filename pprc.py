#!/usr/bin/env python3
import os
import sys
import glob
import subprocess
import argparse
import shutil
import concurrent.futures
import array
import struct
import functools
import ctypes
import tempfile
import hashlib
import re

# ---------------------------------------------------------------------------
# C extension for ~200× speedup over Python map() on LUT application
# ---------------------------------------------------------------------------
# We compile a tiny C function at first use using the system's `cc` (clang on
# macOS, always available via Xcode CLT which Homebrew also requires). The
# compiled .so is cached in /tmp keyed by a hash of the source, so it only
# recompiles if the logic changes. ctypes releases the GIL during C calls,
# meaning ThreadPoolExecutor workers can run the C code in true parallel.

_C_SOURCE = """
#include <stdint.h>

// Apply per-channel LUTs and interleave planar R/G/B → chunky RGB in one pass.
// With -O3 -march=native the compiler auto-vectorises via AVX2 gather instructions
// on supported CPUs, giving near-SIMD throughput without any intrinsics.
void convert_planar_lut(
    const uint16_t* r, const uint16_t* g, const uint16_t* b,
    const uint16_t* rl, const uint16_t* gl, const uint16_t* bl,
    uint16_t* out, int n
) {
    for (int i = 0; i < n; i++) {
        out[i*3    ] = rl[r[i]];
        out[i*3 + 1] = gl[g[i]];
        out[i*3 + 2] = bl[b[i]];
    }
}

// Interleave-only path: no LUT, just planar-to-chunky reorder.
// Compiles down to a vectorised memcpy pattern.
void interleave_planar(
    const uint16_t* r, const uint16_t* g, const uint16_t* b,
    uint16_t* out, int n
) {
    for (int i = 0; i < n; i++) {
        out[i*3    ] = r[i];
        out[i*3 + 1] = g[i];
        out[i*3 + 2] = b[i];
    }
}

// Grayscale luminance path: ITU-R BT.601 coefficients, scaled to 16-bit integer
// arithmetic to avoid any float operations per pixel.
// R*19595 + G*38469 + B*7472 then >> 16 gives ~0.299R + 0.587G + 0.114B.
void interleave_gray_lut(
    const uint16_t* r, const uint16_t* g, const uint16_t* b,
    const uint16_t* rl, const uint16_t* gl, const uint16_t* bl,
    uint16_t* out, int n
) {
    for (int i = 0; i < n; i++) {
        uint32_t rv = rl[r[i]];
        uint32_t gv = gl[g[i]];
        uint32_t bv = bl[b[i]];
        uint16_t gray = (uint16_t)((rv * 19595u + gv * 38469u + bv * 7472u) >> 16);
        out[i*3    ] = gray;
        out[i*3 + 1] = gray;
        out[i*3 + 2] = gray;
    }
}
"""

_cext = None  # ctypes library handle, None until first use

def _load_c_extension():
    """
    Compile and load the C extension on first call; return the cached handle on
    subsequent calls. Returns None if compilation is unavailable (silent fallback
    to pure Python). The .so is stored in /tmp keyed by a hash of the C source
    so it only recompiles when the code changes.
    """
    global _cext
    if _cext is not None:
        return _cext

    src_hash = hashlib.md5(_C_SOURCE.encode()).hexdigest()[:12]
    so_path = os.path.join(tempfile.gettempdir(), f'pprc_convert_{src_hash}.so')

    if not os.path.exists(so_path):
        src_path = so_path + '.c'
        try:
            with open(src_path, 'w') as f:
                f.write(_C_SOURCE)
            result = subprocess.run(
                ['cc', '-O3', '-march=native', '-shared', '-fPIC', '-o', so_path, src_path],
                capture_output=True, timeout=15
            )
            if result.returncode != 0:
                return None  # compiler present but failed; fall back to Python
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None  # no compiler available; fall through to Python path

    try:
        lib = ctypes.CDLL(so_path)
        u16p = ctypes.POINTER(ctypes.c_uint16)
        n = ctypes.c_int

        lib.convert_planar_lut.argtypes  = [u16p, u16p, u16p, u16p, u16p, u16p, u16p, n]
        lib.convert_planar_lut.restype   = None
        lib.interleave_planar.argtypes   = [u16p, u16p, u16p, u16p, n]
        lib.interleave_planar.restype    = None
        lib.interleave_gray_lut.argtypes = [u16p, u16p, u16p, u16p, u16p, u16p, u16p, n]
        lib.interleave_gray_lut.restype  = None

        _cext = lib
        return lib
    except OSError:
        return None

# ---------------------------------------------------------------------------
# LUT construction — built once and cached
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=2)
def _build_base_lut(apply_gamma: bool) -> array.array:
    """
    Build and cache the foundational gamma correction LUT. Runs exactly once per
    unique (apply_gamma) value per process. All subsequent calls return the same
    cached array object at zero cost — no recomputation regardless of how many
    files or channels are processed.

    apply_gamma=True  → standard sRGB 2.2 gamma curve (linear light → display)
    apply_gamma=False → identity mapping (--gamma1 mode, leave data linear)
    """
    if not apply_gamma:
        return array.array('H', range(65536))  # identity: each value maps to itself

    gamma_inv = 1.0 / 2.2
    return array.array('H', (
        int(((i / 65535.0) ** gamma_inv) * 65535.0) for i in range(65536)
    ))

def generate_lut(program_args, plane_data) -> array.array:
    """
    Return a composed 16-bit LUT for the given channel data and CLI flags.
    The gamma curve is always fetched from cache. Auto-level and negate are
    composed into the same single-pass LUT so the C extension only does one
    table lookup per pixel regardless of which transforms are requested.

    Transform order (all composed into one 65536-entry table):
      1. Auto-level  — stretch channel min→0, max→65535
      2. Gamma 2.2   — power curve for display encoding
      3. Negate      — invert for B&W / positive-film modes
    """
    needs_autolevel = program_args.e6 or program_args.bw or program_args.bw_rgb
    needs_negate    = program_args.bw or program_args.bw_rgb

    gamma_lut = _build_base_lut(not program_args.gamma1)  # free after first call

    if not needs_autolevel and not needs_negate:
        # Common path: gamma only — return shared cached array directly (zero allocation)
        return gamma_lut

    if needs_autolevel:
        min_val = min(plane_data)  # C-level min/max scan, very fast
        max_val = max(plane_data)
        if max_val > min_val:
            diff = max_val - min_val
            # Compose auto-level + gamma (+ optional negate) in a single pass.
            # Integer // avoids float overhead; clamp replaces conditional branches.
            if needs_negate:
                return array.array('H', (
                    65535 - gamma_lut[max(0, min(65535, (i - min_val) * 65535 // diff))]
                    for i in range(65536)
                ))
            else:
                return array.array('H', (
                    gamma_lut[max(0, min(65535, (i - min_val) * 65535 // diff))]
                    for i in range(65536)
                ))

    if needs_negate:
        return array.array('H', (65535 - gamma_lut[i] for i in range(65536)))

    return gamma_lut  # flat channel edge case falls through here

# ---------------------------------------------------------------------------
# TIFF writer — purely in-memory, no disk I/O
# ---------------------------------------------------------------------------

def create_tiff_16bit_rgb_bytes(width, height, data_bytes) -> bytes:
    """
    Construct a valid uncompressed 16-bit RGB TIFF byte string entirely in memory.
    Writes a minimal TIFF Image File Directory (IFD) followed by the raw pixel data.
    The resulting bytes can be written atomically to disk by the caller.
    """
    # 'II' = little-endian, 0x002A = TIFF magic number 42
    header = b"II\x2a\x00"
    header += struct.pack("<I", 8)  # IFD starts immediately after the 8-byte header

    tags = []
    def add_tag(tag, type_, count, val_or_offset):
        # Each IFD entry: tag(2) + type(2) + count(4) + value/offset(4) = 12 bytes
        # type 3 = SHORT (uint16), type 4 = LONG (uint32)
        tags.append(struct.pack("<HHII", tag, type_, count, val_or_offset))

    add_tag(256, 3, 1, width)    # ImageWidth
    add_tag(257, 3, 1, height)   # ImageLength

    # BitsPerSample has 3 values (one per channel) so it can't fit inline — use an offset.
    # Layout: header(8) + entry_count(2) + 10 entries × 12 bytes + next_IFD(4) = 134 bytes
    bps_offset = 8 + 2 + 10 * 12 + 4
    add_tag(258, 3, 3, bps_offset)   # BitsPerSample → (16, 16, 16) stored at bps_offset

    add_tag(259, 3, 1, 1)   # Compression: 1 = uncompressed
    add_tag(262, 3, 1, 2)   # PhotometricInterpretation: 2 = RGB

    data_offset = bps_offset + 6  # pixel data starts right after the 6-byte BPS array
    add_tag(273, 4, 1, data_offset)  # StripOffsets
    add_tag(277, 3, 1, 3)            # SamplesPerPixel
    add_tag(278, 4, 1, height)       # RowsPerStrip (single strip for simplicity)
    add_tag(279, 4, 1, len(data_bytes))  # StripByteCounts
    add_tag(284, 3, 1, 1)            # PlanarConfiguration: 1 = chunky (RGBRGB…)

    ifd      = struct.pack("<H", len(tags)) + b"".join(tags) + struct.pack("<I", 0)
    bps_data = struct.pack("<HHH", 16, 16, 16)

    return header + ifd + bps_data + data_bytes

# ---------------------------------------------------------------------------
# Main conversion — C path with Python fallback
# ---------------------------------------------------------------------------

def convert_planar_raw(raw_bytes, program_args, w, h) -> bytes:
    """
    Convert a single planar raw file's bytes to chunky 16-bit RGB pixel data.

    The planar format stores all red values first, then green, then blue:
      [ R0 R1 … Rn | G0 G1 … Gn | B0 B1 … Bn ]

    This function reorders them to the standard chunky (interleaved) layout:
      [ R0 G0 B0 | R1 G1 B1 | … | Rn Gn Bn ]

    Gamma correction, auto-levelling, and inversion are applied via the LUT
    during the same single pass through the data, minimising memory bandwidth.
    """
    pixels = w * h

    # Decode the raw bytes into a flat array of uint16 values
    data = array.array('H')
    data.frombytes(raw_bytes)

    # Slice out the three contiguous planes — these are views, not copies
    R = data[0:pixels]
    G = data[pixels:2*pixels]
    B = data[2*pixels:3*pixels]

    needs_lut = not program_args.unadjusted and (
        not program_args.gamma1 or program_args.e6 or program_args.bw or program_args.bw_rgb
    )

    # Allocate the output buffer: pixels × 3 channels × 2 bytes per uint16
    out = array.array('H')
    out.frombytes(b'\x00' * pixels * 6)

    cext = _load_c_extension()

    if cext is not None:
        # ---- Fast path: C extension (ctypes releases GIL → true thread parallelism) ----
        u16  = ctypes.c_uint16
        u16p = ctypes.POINTER(u16)

        r_buf   = (u16 * pixels).from_buffer(R)
        g_buf   = (u16 * pixels).from_buffer(G)
        b_buf   = (u16 * pixels).from_buffer(B)
        out_buf = (u16 * (pixels * 3)).from_buffer(out)

        if needs_lut:
            # LUT generation is Python-side (negligible after first call due to cache),
            # the actual application of 6M lookups happens in C.
            r_lut = generate_lut(program_args, R)
            g_lut = generate_lut(program_args, G)
            b_lut = generate_lut(program_args, B)

            rl_buf = (u16 * 65536).from_buffer(r_lut)
            gl_buf = (u16 * 65536).from_buffer(g_lut)
            bl_buf = (u16 * 65536).from_buffer(b_lut)

            if program_args.bw:
                # Grayscale: luminance coefficients applied in C integer arithmetic
                cext.interleave_gray_lut(r_buf, g_buf, b_buf, rl_buf, gl_buf, bl_buf, out_buf, pixels)
            else:
                cext.convert_planar_lut(r_buf, g_buf, b_buf, rl_buf, gl_buf, bl_buf, out_buf, pixels)
        else:
            # No transforms at all (--unadjusted or --gamma1 with no other flags)
            cext.interleave_planar(r_buf, g_buf, b_buf, out_buf, pixels)

    else:
        # ---- Slow path: pure Python fallback (no system compiler available) ----
        if needs_lut:
            r_lut = generate_lut(program_args, R)
            g_lut = generate_lut(program_args, G)
            b_lut = generate_lut(program_args, B)

            R = array.array('H', map(r_lut.__getitem__, R))
            G = array.array('H', map(g_lut.__getitem__, G))
            B = array.array('H', map(b_lut.__getitem__, B))

        if program_args.bw:
            gray = array.array('H', (
                int(R[i]*0.299 + G[i]*0.587 + B[i]*0.114) for i in range(pixels)
            ))
            R = G = B = gray

        # Stride assignment: C-level loop inside array module, much faster than a Python loop
        out[0::3] = R
        out[1::3] = G
        out[2::3] = B

    return out.tobytes()


OUTPUT_DIR = "out"

BYTE_SIZE_TO_DIMENSIONS = {
    "36000000": "3000x2000",
    "36000016": "3000x2000+16",
    "20250000": "2250x1500",
    "20250016": "2250x1500+16",
    "9000000": "1500x1000",
    "9000016": "1500x1000+16"
}

def check_dependencies(program_args):
    if program_args.no_dependency_check:
        print("Skipping Dependency Check...")
        return

    if not shutil.which("negfix8") and not program_args.no_negfix:
        exit_with_error("'negfix8' doesn't seem to exist, please install it or run without --negfix.")

def exit_with_error(message, item=None):
    if item:
        print(f"ERROR: {message} {item}", file=sys.stderr)
    else:
        print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)

def scan_directory_for_files():
    raw_files = glob.glob('*.raw')
    if not raw_files:
        exit_with_error("No .raw files found in the current directory \nPlease run this script from the same directory where you have saved your planar .raw files from TLXClientDemo")
    else:
        print(f"Found {len(raw_files)} raw files in current directory...")
        return raw_files

def natural_sort_key(s: str) -> tuple:
    """Converts string into a sequence of (number, string) tuples for correct numeric sorting."""
    return tuple(int(c) if c.isdigit() else c.lower() for c in re.sub('([0-9]+)', r' \1 ', s).split())

def parse_raw_header(raw_file):
    """Attempt to read width/height/bpp directly from the 16-byte binary header
    that TLXClientDemo optionally prepends to planar raw files."""
    size_in_bytes = os.path.getsize(raw_file)
    if size_in_bytes < 16:
        return None
    try:
        with open(raw_file, 'rb') as f:
            header_data = f.read(16)
        if len(header_data) == 16:
            header_size, width, height, bpp = struct.unpack('<IIII', header_data)
            if header_size == 16 and bpp == 48:
                pixel_count = width * height
                if (size_in_bytes - 16) / pixel_count == 6:
                    return f"{width}x{height}+16"
    except Exception:
        pass
    return None

def check_raw_file_sizes(raw_files, program_args):
    data = {}
    bad_files = []
    
    for raw_file in raw_files:
        size_in_bytes = os.path.getsize(raw_file)
        dimensions_for_convert = None
        
        if program_args.dimensions and len(program_args.dimensions.split("x")) == 2:
            # Case 1: The user manually provided raw dimensions via CLI flag
            try:
                width, height = map(int, program_args.dimensions.split("x"))
                pixel_count = width * height
                if size_in_bytes / pixel_count == 6:
                    dimensions_for_convert = f"{width}x{height}"
                elif (size_in_bytes - 16) / pixel_count == 6:
                    dimensions_for_convert = f"{width}x{height}+16"
            except ValueError:
                pass
        else:
            # Case 2: Attempt to dynamically read the exact dimensions from the raw file binary header
            dimensions_for_convert = parse_raw_header(raw_file)
            
            # Case 3: If no header exists, fallback strictly to the legacy size mapping
            if not dimensions_for_convert:
                dimensions_for_convert = BYTE_SIZE_TO_DIMENSIONS.get(str(size_in_bytes))

        if not dimensions_for_convert:
            bad_files.append(raw_file)
            print(f"{raw_file} could not be parsed - please export via TLXClientDemo in \"Planar\" mode (or specify dimensions via --dimensions option)", file=sys.stderr)
        else:
            data[raw_file] = {
                "size": dimensions_for_convert
            }
            
    valid_file_count = len(data)
    if valid_file_count == 0:
        exit_with_error("Sorry, no valid or parseable .raw files were found in the current directory.")
    elif valid_file_count == len(raw_files):
        print(f"All {valid_file_count} files in the current directory were successfully parsed...")
    else:
        is_are = "is" if len(bad_files) == 1 else "are"
        print(f"{valid_file_count} files will be converted but {len(raw_files)-valid_file_count} ({','.join(bad_files)}) {is_are} invalid or could not be parsed...")

    return data

def convert_raw_to_tiff(name, size_parameter, program_args):
    """
    Reads the raw planar bytes for a single file and returns the fully converted
    16-bit RGB TIFF as an in-memory bytes object. No temporary files are written.
    The caller is responsible for writing results to disk in chronological order.
    """
    base_name = os.path.splitext(name)[0]
    destination_file = f"{base_name}.tif"
    
    # If no negfix logic will be applied subsequently, the final path goes straight
    # into the output directory rather than being left alongside the source file.
    no_negfix = program_args.no_negfix or program_args.e6 or program_args.unadjusted or program_args.bw or program_args.bw_rgb
    if no_negfix:
        destination_file = os.path.join(program_args.output_dir, destination_file)
        
    # Parse "WxH" or "WxH+offset" size string produced by check_raw_file_sizes
    parts = size_parameter.split('+')
    offset_bytes = int(parts[1]) if len(parts) > 1 else 0
    w, h = map(int, parts[0].split('x'))
    
    with open(name, 'rb') as f:
        # Seek past the optional binary header without reading it into memory
        f.seek(offset_bytes)
        # Exact read: w*h pixels × 3 channels × 2 bytes per 16-bit sample
        raw_bytes = f.read(w * h * 6)
        
    # Convert planar (RRR..GGG..BBB..) → chunky (RGBRGB..) with all transforms applied
    converted_bytes = convert_planar_raw(raw_bytes, program_args, w, h)
    
    # Build the complete TIFF file bytes in memory — no disk I/O until final write
    tiff_bytes = create_tiff_16bit_rgb_bytes(w, h, converted_bytes)
    
    return name, tiff_bytes, destination_file

def convert_raw_files_to_tiff(data, program_args):
    action_label = "CONVERTING"
    if program_args.gamma1:
        action_label += " (without gamma adjustment)"
        
    sys.stdout.write(f"{action_label}: ")
    sys.stdout.flush()
    
    results = []
    
    # ThreadPoolExecutor is ideal here: ctypes releases the GIL during the C extension
    # call, so threads run the LUT+interleave work in true parallel across all cores.
    # Unlike ProcessPoolExecutor, threads share memory so there's zero pickle or IPC
    # overhead — no 36MB TIFF needs to be serialised between processes per image.
    max_workers = os.cpu_count() or 4
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(convert_raw_to_tiff, item, info["size"], program_args): item for item, info in data.items()}
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            try:
                res = future.result()
                results.append(res)
                sys.stdout.write(" ▢ ")
                sys.stdout.flush()
            except Exception as exc:
                exit_with_error(f"Error converting a file from a raw to a tiff: {exc}", item)

    # Sort by original filename so writes to disk happen in filename order.
    # This ensures filesystem mtime increases monotonically, which guarantees
    # correct chronological ordering when imported into apps like Apple Photos.
    results.sort(key=lambda x: natural_sort_key(x[0]))
    
    tifs = []
    for name, tiff_bytes, destination_file in results:
        # Atomic write: all TIFF data was held in RAM, now flushed to disk in order
        with open(destination_file, 'wb') as f:
            f.write(tiff_bytes)
        # Track final tif path for the optional negfix8 pass
        no_negfix = program_args.no_negfix or program_args.e6 or program_args.unadjusted or program_args.bw or program_args.bw_rgb
        tifs.append(destination_file if no_negfix else f"{os.path.splitext(name)[0]}.tif")

    return tifs

def adjust_tifs_with_negfix8(tifs, program_args):
    sys.stdout.write("ADJUSTING: ")
    sys.stdout.flush()
    
    def process_negfix(tif):
        output_dest = f"{program_args.output_dir}/{tif}"
        temp_dest = f"{output_dest}.tmp"
        cmd = f'negfix8 -cs "{tif}" "{temp_dest}"'
        try:
            subprocess.run(cmd, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return tif, temp_dest, output_dest
        except subprocess.CalledProcessError:
            return tif, None, None

    results = []
    max_workers = os.cpu_count() or 4
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_negfix, tif): tif for tif in tifs}
        for future in concurrent.futures.as_completed(futures):
            tif = futures[future]
            try:
                res = future.result()
                results.append(res)
                sys.stdout.write(" ▢ ")
                sys.stdout.flush()
            except Exception as exc:
                print(f"\nError converting {tif}", file=sys.stderr)
                
    # Make sure negfix 8 output writes happen sequentially based on original filename
    # to maintain chronological sequence for imports.
    result = []
    results.sort(key=lambda x: natural_sort_key(x[0]))
    for tif, temp_dest, output_dest in results:
        if temp_dest and output_dest:
            shutil.move(temp_dest, output_dest)
            result.append(tif)
        else:
            print(f"\nError converting {tif} to {program_args.output_dir}/{tif}", file=sys.stderr)
            
    return result

def main():
    parser = argparse.ArgumentParser(description="A script to convert Pakon F-135+ Planar RAW scans from TLXClientDemo into usable files via native python memory conversion and NegFix8", add_help=False)
    
    parser.add_argument('-h', '--help', action='help', default=argparse.SUPPRESS, help='Show this help message and exit.')
    parser.add_argument('-V', '--version', action='version', version='0.0.13')
    parser.add_argument('--output-dir', default=OUTPUT_DIR, dest='output_dir', metavar='[dir]', help=f'Override the default the output sub-directory of "{OUTPUT_DIR}"')
    parser.add_argument('--negfix', action='store_false', dest='no_negfix', help='Run negfix8 to balance and invert colors (default: skipped)')
    parser.add_argument('--no-negfix', action='store_true', default=True, help=argparse.SUPPRESS) # Keep as hidden for compatibility
    parser.add_argument('--no-dependency-check', action='store_true', help='Avoid checking for dependencies')
    parser.add_argument('--dimensions', metavar='[width]x[height]', help='Manually specify pixel dimensions of raw file (useful for xpan, etc) format like "3000x2000"')
    parser.add_argument('--e6', action='store_true', help='Apply an -auto-level algorithm on files. Useful when scanning "Film Color: Positive" in TLXClientDemo')
    parser.add_argument('--unadjusted', action='store_true', help='Do not auto-level, basically just converts planar raws to tifs')
    parser.add_argument('--bw', action='store_true', help='Natively do the following: invert, auto-level, and save in grey-scale colorspace')
    parser.add_argument('--bw-rgb', dest='bw_rgb', action='store_true', help='Natively do the following: invert, auto-level, and save in RGB colorspace')
    parser.add_argument('--gamma1', action='store_true', help='Do not apply a 2.2 gamma correction when converting the raw file, instead leaving it "linear", with a 1.0 gamma')
    
    args = parser.parse_args()
    
    check_dependencies(args)
    
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)
        
    raw_files = scan_directory_for_files()
    data = check_raw_file_sizes(raw_files, args)
    tifs = convert_raw_files_to_tiff(data, args)
    
    sys.stdout.write("\n")
    
    if args.no_negfix or args.e6 or args.unadjusted or args.bw or args.bw_rgb:
        verb = "raw"
        if args.e6:
            verb = "auto-leveled"
        elif args.unadjusted:
            verb = "unadjusted"
        elif args.bw:
            verb = "inverted and auto-leveled greyscale"
        elif args.bw_rgb:
            verb = "inverted and auto-leveled RGB"
            
        file_word = "file" if len(tifs) == 1 else "files"
        print(f"Done. {len(tifs)} {file_word} saved to the '{args.output_dir}' subdirectory as a {verb} TIFF.")
    else:
        print("Converted raw files to tifs, inverting and balancing with negfix8...")
        converted_files = adjust_tifs_with_negfix8(tifs, args)
        file_word = "file" if len(converted_files) == 1 else "files"
        sys.stdout.write("\n")
        print(f"Done. {len(converted_files)} {file_word} saved to the '{args.output_dir}' subdirectory as processed TIFF.")

if __name__ == "__main__":
    # ThreadPoolExecutor is used for conversion, so the __main__ guard
    # is less critical than with ProcessPoolExecutor 'spawn', but still
    # good practice for script entry points.
    main()
