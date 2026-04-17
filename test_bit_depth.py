import os
import glob
import shutil
import subprocess

def run_test(args, test_name):
    os.chdir("testraw")
    
    raw_files = glob.glob('*.raw')
    if not raw_files:
        print("No .raw files found in testraw dir.")
        os.chdir("..")
        return

    out_dir = f"out_{test_name}"
    
    # We remove the directory if it exists and let pprc.py create it
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
            
    # Run the converter for all raw files in the directory using the built-in --validate flag
    print(f"\n--- Running pprc with --validate on {test_name} ---")
    subprocess.run(["python3", "../pprc.py", "--validate", "--output-dir", out_dir] + args, check=True)
            
    os.chdir("..")

def test_bit_depth_unadjusted():
    run_test(["--unadjusted"], "unadjusted")

def test_bit_depth_default():
    run_test([], "default")

if __name__ == "__main__":
    test_bit_depth_unadjusted()
    test_bit_depth_default()
    print("\nAll tests passed successfully!")
