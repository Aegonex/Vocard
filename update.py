"""MIT License

Copyright (c) 2023 - present Vocard Development

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import argparse
import os
import sys
import requests
import zipfile
import shutil
import subprocess
import tempfile
from io import BytesIO

from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
__version__ = "v2.7.3"

load_dotenv(os.path.join(ROOT_DIR, ".env"), override=False)

# URLs for update and migration
PYTHON_CMD_NAME = os.path.basename(sys.executable)
GITHUB_API_URL = "https://api.github.com/repos/ChocoMeow/Vocard/releases/latest"
VOCARD_URL = "https://github.com/ChocoMeow/Vocard/archive/"
MIGRATION_SCRIPT_URL = f"https://raw.githubusercontent.com/ChocoMeow/Vocard-Magration/main/{__version__}.py"
IGNORE_FILES = [".env", "settings.json", "logs", "last-session.json"]
CUSTOM_SETTINGS_PATH = None

if custom_settings_file := os.getenv("SETTINGS_FILE"):
    custom_settings_path = os.path.abspath(
        custom_settings_file
        if os.path.isabs(custom_settings_file)
        else os.path.join(ROOT_DIR, custom_settings_file)
    )
    try:
        relative_settings_path = os.path.relpath(custom_settings_path, ROOT_DIR)
        if relative_settings_path != os.pardir and not relative_settings_path.startswith(os.pardir + os.sep):
            path_parts = relative_settings_path.split(os.sep)
            if len(path_parts) == 1:
                if relative_settings_path not in IGNORE_FILES:
                    IGNORE_FILES.append(relative_settings_path)
            else:
                CUSTOM_SETTINGS_PATH = custom_settings_path
    except ValueError:
        pass


def backup_custom_settings() -> str | None:
    if not CUSTOM_SETTINGS_PATH or not os.path.isfile(CUSTOM_SETTINGS_PATH):
        return None

    descriptor, backup_path = tempfile.mkstemp(prefix="vocard-settings-", suffix=".json")
    os.close(descriptor)
    shutil.copy2(CUSTOM_SETTINGS_PATH, backup_path)
    return backup_path


def restore_custom_settings(backup_path: str | None) -> None:
    if not backup_path:
        return

    try:
        os.makedirs(os.path.dirname(CUSTOM_SETTINGS_PATH), exist_ok=True)
        shutil.copy2(backup_path, CUSTOM_SETTINGS_PATH)
    except Exception:
        print(f"{bcolors.FAIL}Could not restore custom settings. Backup: {backup_path}{bcolors.ENDC}")
        raise
    else:
        os.remove(backup_path)

class bcolors:
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    OKGREEN = '\033[92m'
    ENDC = '\033[0m'

def check_version(with_msg=False):
    """Check for the latest version of the project.

    Args:
        with_msg (bool): option to print the message.

    Returns:
        str: the latest version.
    """
    try:
        response = requests.get(GITHUB_API_URL, timeout=10)
        response.raise_for_status()
        payload = response.json()
        latest_version = payload.get("name", __version__)
        if not isinstance(latest_version, str) or not latest_version:
            latest_version = __version__
    except (requests.RequestException, ValueError, TypeError) as exc:
        if with_msg:
            print(
                f"{bcolors.WARNING}Could not check for updates; continuing with "
                f"{__version__}. Reason: {exc}{bcolors.ENDC}"
            )
        return __version__

    if with_msg:
        msg = (
            f"{bcolors.OKGREEN}Your bot is up-to-date! - {latest_version}{bcolors.ENDC}" 
            if latest_version == __version__
            else f"{bcolors.WARNING}Your bot is not up-to-date! The latest version is {latest_version} and you are currently running version {__version__}\nRun `{PYTHON_CMD_NAME} update.py -l` to update your bot!{bcolors.ENDC}"
        )
        print(msg)
    return latest_version

def download_file(version=None):
    """Download the latest version of the project.

    Args:
        version (str): the version to download. If None, download the latest version.

    Returns:
        Response: the downloaded zip file content.
    """
    version = version if version else check_version()
    print(f"Downloading Vocard version: {version}")
    response = requests.get(VOCARD_URL + version + ".zip")
    if response.status_code == 404:
        print(f"{bcolors.FAIL}Warning: Version not found!{bcolors.ENDC}")
        exit(1)
    print("Download Completed")
    return response

def install(response, version):
    """Install the downloaded version of the project.

    Args:
        response (Response): the downloaded zip file content.
        version (str): the version to install.
    """
    user_input = input(
        f"{bcolors.WARNING}--------------------------------------------------------------------------\n"
        "Note: Before proceeding, please ensure that there are no personal files or\n"
        "sensitive information in the directory you're about to delete. This action\n"
        "is irreversible, so it's important to double-check that you're making the \n"
        f"right decision. {bcolors.ENDC} Continue with caution? (Y/n) "
    )
        
    if user_input.lower() in ["y", "yes"]:
        print("Installing ...")
        settings_backup = backup_custom_settings()
        try:
            zfile = zipfile.ZipFile(BytesIO(response.content))
            zfile.extractall(ROOT_DIR)

            # Remove 'v' from the version string for folder name.
            version_without_v = version.replace("v", "")
            source_dir = os.path.join(ROOT_DIR, f"Vocard-{version_without_v}")
            if os.path.exists(source_dir):
                for filename in os.listdir(ROOT_DIR):
                    if filename in IGNORE_FILES + [f"Vocard-{version_without_v}"]:
                        continue

                    filename_path = os.path.join(ROOT_DIR, filename)
                    if os.path.isdir(filename_path):
                        shutil.rmtree(filename_path)
                    else:
                        os.remove(filename_path)
                for filename in os.listdir(source_dir):
                    shutil.move(os.path.join(source_dir, filename), os.path.join(ROOT_DIR, filename))
                os.rmdir(source_dir)
        finally:
            restore_custom_settings(settings_backup)
        print(f"{bcolors.OKGREEN}Version {version} installed Successfully! Run `{PYTHON_CMD_NAME} main.py` to start your bot{bcolors.ENDC}")
    else:
        print("Update canceled!")

def run_migration():
    """Download, execute, and remove the migration script."""
    confirm = input(
        f"{bcolors.WARNING}WARNING: Please ensure you have taken a backup before proceeding.\n"
        f"Are you sure you want to run the migration? (Y/n): {bcolors.ENDC} "
    )
    if confirm.lower() not in ["y", "yes"]:
        print("Migration canceled!")
        return

    print("Downloading migration script...")
    response = requests.get(MIGRATION_SCRIPT_URL)
    if response.status_code != 200:
        print(f"{bcolors.FAIL}Failed to download migration script. Status code: {response.status_code}{bcolors.ENDC}")
        exit(1)

    migration_filename = "temp_migration.py"
    with open(migration_filename, "w", encoding="utf-8") as f:
        f.write(response.text)

    print("Executing migration script...")
    try:
        subprocess.run([PYTHON_CMD_NAME, migration_filename], check=True)
        print(f"{bcolors.OKGREEN}Migration script executed successfully.{bcolors.ENDC}")
    except subprocess.CalledProcessError as e:
        print(f"{bcolors.FAIL}Migration script execution failed: {e}{bcolors.ENDC}")
    finally:
        if os.path.exists(migration_filename):
            os.remove(migration_filename)
            print("Temporary migration script deleted.")

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description='Update and migration script for Vocard.')
    parser.add_argument('-c', '--check', action='store_true', help='Check the current version of the Vocard')
    parser.add_argument('-v', '--version', type=str, help='Install the specified version of the Vocard')
    parser.add_argument('-l', '--latest', action='store_true', help='Install the latest version of the Vocard from Github')
    parser.add_argument('-b', '--beta', action='store_true', help='Install the beta version of the Vocard from Github')
    parser.add_argument('-m', '--migration', action='store_true', help='Download and run the migration script from Github')
    return parser.parse_args()

def main():
    """Main function."""
    args = parse_args()

    if args.check:
        check_version(with_msg=True)
        
    elif args.version:
        version = args.version
        response = download_file(version)
        install(response, version)
        
    elif args.latest:
        response = download_file()
        version = check_version()
        install(response, version)
        
    elif args.beta:
        response = download_file("refs/heads/beta")
        install(response, "beta")
        
    elif args.migration:
        run_migration()
    else:
        print(f"{bcolors.FAIL}No arguments provided. Run `{PYTHON_CMD_NAME} update.py -h` for help.{bcolors.ENDC}")

if __name__ == "__main__":
    main()
