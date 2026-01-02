#!/usr/bin/env python3
import os
import zipfile
import shutil
from pathlib import Path
import re

def increment_version():
    pyproject_path = Path("pyproject.toml")
    if not pyproject_path.exists():
        print("pyproject.toml not found")
        return
    
    content = pyproject_path.read_text()
    version_match = re.search(r'version = "(\d+)\.(\d+)\.(\d+)"', content)
    if not version_match:
        print("Version not found in pyproject.toml")
        return
    
    major, minor, patch = map(int, version_match.groups())
    patch += 1
    new_version = f"{major}.{minor}.{patch}"
    
    new_content = re.sub(r'version = "\d+\.\d+\.\d+"', f'version = "{new_version}"', content)
    pyproject_path.write_text(new_content)
    print(f"Version incremented to {new_version}")

def create_zip():
    current_dir = Path.cwd()
    parent_dir = current_dir.name
    zip_name = f"{parent_dir}.sdkmod"
    
    with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.write("__init__.py", f"{parent_dir}/__init__.py")
        zf.write("pyproject.toml", f"{parent_dir}/pyproject.toml")
    
    print(f"Created {zip_name}")
    return zip_name

def copy_to_game():
    current_dir = Path.cwd()
    parent_dir = current_dir.parent.name
    zip_name = f"{current_dir.name}.sdkmod"
    
    if parent_dir == "willow1_mods":
        dest = Path("C:/Program Files (x86)/Steam/steamapps/common/Borderlands/sdk_mods")
    elif parent_dir == "willow2_mods":
        dest = Path("C:/Program Files (x86)/Steam/steamapps/common/BorderlandsPreSequel/sdk_mods")
    else:
        print(f"Unknown parent directory: {parent_dir}")
        return
    
    if dest.exists():
        shutil.copy2(zip_name, dest / zip_name)
        print(f"Copied to {dest / zip_name}")
    else:
        print(f"Destination not found: {dest}")

if __name__ == "__main__":
    increment_version()
    create_zip()
    copy_to_game()