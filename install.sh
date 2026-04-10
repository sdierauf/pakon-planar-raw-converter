#!/bin/bash

# Ensure /usr/local/bin exists
sudo mkdir -p /usr/local/bin

echo "Installing PPRC to /usr/local/bin..."
sudo ln -sf "$PWD/pprc.py" /usr/local/bin/pprc

# Make sure the script is executable
chmod +x "$PWD/pprc.py"

echo "Installed successfully!"
echo "You can now run 'pprc' from any folder."
