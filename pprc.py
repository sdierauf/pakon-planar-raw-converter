#!/usr/bin/env python3
import os
import sys
import glob
import subprocess
import argparse
import shutil
import concurrent.futures
import struct

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
        print("Skipping Dependancy Check...")
        return

    if sys.platform == "win32":
        if not shutil.which("magick"):
            exit_with_error("'magick' from ImageMagick doesn't seem to exist, please install it")
    else:
        if not shutil.which("convert"):
            exit_with_error("'convert' from ImageMagick doesn't seem to exist, please install it")

    if not shutil.which("negfix8"):
        exit_with_error("'negfix8' doesn't seem to exist, please install it")

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

def parse_raw_header(raw_file):
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
            dimensions_for_convert = parse_raw_header(raw_file)
            if not dimensions_for_convert:
                dimensions_for_convert = BYTE_SIZE_TO_DIMENSIONS.get(str(size_in_bytes))

        if not dimensions_for_convert:
            bad_files.append(raw_file)
            print(f"{raw_file} is the wrong size - please export via TLXClientDemo in \"Planar\" mode at \"Original height and width\" (or specify dimensions via --dimensions option)", file=sys.stderr)
        else:
            data[raw_file] = {
                "size": dimensions_for_convert
            }
            
    valid_file_count = len(data)
    if valid_file_count == 0:
        exit_with_error("Sorry, no .raw files in the current directory are the correct size.")
    elif valid_file_count == len(raw_files):
        print(f"All {valid_file_count} files in the current directory are a correct size...")
    else:
        is_are = "is" if len(bad_files) == 1 else "are"
        print(f"{valid_file_count} files will be converted but {len(raw_files)-valid_file_count} ({','.join(bad_files)}) {is_are} the wrong size...")

    return data

def convert_raw_to_tiff(name, size_parameter, program_args):
    base_name = os.path.splitext(name)[0]
    destination_file = f"{base_name}.tif"
    no_negfix = program_args.no_negfix or program_args.e6 or program_args.unadjusted or program_args.bw or program_args.bw_rgb
    
    extra = ""
    if no_negfix:
        destination_file = os.path.join(program_args.output_dir, destination_file)
        
    if program_args.e6:
        extra += " -auto-level"
    elif program_args.bw:
        extra += " -negate -auto-level -colorspace Gray"
    elif program_args.bw_rgb:
        extra += " -negate -auto-level"
        
    gamma_str = "" if program_args.gamma1 else "-gamma 2.2"
    
    temp_file = destination_file + ".tmp"
    cmd = f'convert -size {size_parameter} -depth 16 -interlace plane rgb:"{name}" {gamma_str} {extra} -interlace none tif:"{temp_file}"'
    
    if sys.platform == "win32":
        cmd = f'magick {cmd}'
        
    subprocess.run(cmd, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return name, temp_file, destination_file

def convert_raw_files_to_tiff(data, program_args):
    action_label = "CONVERTING"
    if program_args.gamma1:
        action_label += " (without gamma adjustment)"
        
    sys.stdout.write(f"{action_label}: ")
    sys.stdout.flush()
    
    results = []
    
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

    # Sort the results by original filename to make sure that the writes happen sequentially.
    # This guarantees that the filesystem creation/modification times happen in filename order,
    # ensuring they will appear in the correct sequence when imported into apps like Apple Photos.
    results.sort(key=lambda x: x[0])
    
    tifs = []
    for name, temp_file, destination_file in results:
        shutil.move(temp_file, destination_file)
        # We append destination_file if we are skipping negfix, else just the base filename
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
    results.sort(key=lambda x: x[0])
    for tif, temp_dest, output_dest in results:
        if temp_dest and output_dest:
            shutil.move(temp_dest, output_dest)
            result.append(tif)
        else:
            print(f"\nError converting {tif} to {program_args.output_dir}/{tif}", file=sys.stderr)
            
    return result

def main():
    parser = argparse.ArgumentParser(description="A script to convert Pakon F-135+ Planar RAW scans from TLXClientDemo into usable files via ImageMagick's Convert and NegFix8", add_help=False)
    
    parser.add_argument('-h', '--help', action='help', default=argparse.SUPPRESS, help='Show this help message and exit.')
    parser.add_argument('-V', '--version', action='version', version='0.0.13')
    parser.add_argument('--output-dir', default=OUTPUT_DIR, dest='output_dir', metavar='[dir]', help=f'Override the default the output sub-directory of "{OUTPUT_DIR}"')
    parser.add_argument('--no-negfix', action='store_true', help='Skip running negfix8, leaving you with raw .tiff files for further processing with another tool')
    parser.add_argument('--no-dependency-check', action='store_true', help='Avoid checking for dependencies')
    parser.add_argument('--dimensions', metavar='[width]x[height]', help='Manually specify pixel dimensions of raw file (useful for xpan, etc) format like "3000x2000"')
    parser.add_argument('--e6', action='store_true', help='Skip running negfix8, apply ImageMagick\'s -auto-level on files. Useful when scanning "Film Color: Positive" in TLXClientDemo')
    parser.add_argument('--unadjusted', action='store_true', help='Skip running negfix8 and do not auto-level, basically just converts planar raws to tifs')
    parser.add_argument('--bw', action='store_true', help='Skip running negfix8, instead do the following via ImageMagick: invert, auto-level, and save in grey-scale colorspace')
    parser.add_argument('--bw-rgb', dest='bw_rgb', action='store_true', help='Skip running negfix8, instead do the following via ImageMagick: invert, auto-level, and save in RGB colorspace')
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
    main()
