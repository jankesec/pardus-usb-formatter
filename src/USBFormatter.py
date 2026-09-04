#!/usr/bin/env python3

import os
import uuid
import signal
import subprocess
import json
import sys
import re
import stat

stopWriting = False

import argparse

def receiveSignal(number, frame):
    global stopWriting
    stopWriting = True
    return


signal.signal(signal.SIGTERM, receiveSignal)

parser = argparse.ArgumentParser(
        prog='Pardus USB Formatter',
        description='Format USB flash drives',
        epilog='USB Format tool for pardus'
)

parser.add_argument('-t', '--type', default="FAT32")
parser.add_argument('-d', '--device', required=True)
parser.add_argument('-l', '--label',default="")
parser.add_argument('-c', '--crypt')
parser.add_argument('-f', '--fill', default=False, action='store_true')
parser.add_argument('-g', '--gpt', default=False, action='store_true')

args = parser.parse_args()


def execute(command):
    subprocess.call(command)
    subprocess.call(["sync"])


def create_luks(disk, name, password):
    cmds = [
        ["cryptsetup", "luksFormat", disk, "-"],
        ["cryptsetup", "luksOpen", disk, name, "-"]
    ]
    for cmd in cmds:
        p = subprocess.Popen(cmd,
            stdin=subprocess.PIPE,
        )
        p.communicate(input=password.encode())
        if p.returncode != 0:
            raise Exception("Failed to run command: {}".format(" ".join(cmd)))

def find_mounts(disk):
    sp = subprocess.run(
        ["lsblk", "-J", f"/dev/{disk}"],
        stdout=subprocess.PIPE
    )
    data = sp.stdout.decode("utf-8")
    data = json.loads(data)
    ret = []
    names = []
    for blocks in data.get("blockdevices", []):
        ret += blocks.get("mountpoints", [])
        names.append(blocks.get("name"))
        for child in blocks.get("children", []):
            ret += child.get("mountpoints", [])
            names.append(child.get("name"))
            for child in child.get("children", []):
                ret += child.get("mountpoints", [])
                names.append(child.get("name"))
    return {"mounts": ret, "names": names}


def validate_device_and_label(raw_device, raw_label):
    if raw_label:
        if any(c in raw_label for c in "\n\r\0"):
            sys.stderr.write("Error: Disk label contains invalid control characters.\n")
            sys.exit(1)

    if not raw_device or not isinstance(raw_device, str):
        sys.stderr.write("Error: Device argument cannot be empty.\n")
        sys.exit(1)

    dev_name = raw_device.strip()
    if dev_name.startswith("/dev/"):
        dev_name = dev_name[5:]

    # 1. Enforce strict device name format (no path traversal '..', slashes, or shell chars)
    if not re.match(r"^[a-zA-Z0-9_-]+$", dev_name):
        sys.stderr.write(f"Error: Invalid device name format: '{raw_device}'.\n")
        sys.exit(1)

    canonical_path = os.path.realpath(f"/dev/{dev_name}")
    canonical_dev_dir = os.path.realpath("/dev")
    if not (canonical_path.startswith(canonical_dev_dir + "/") or canonical_path == canonical_dev_dir):
        sys.stderr.write(f"Error: Device path '{canonical_path}' is outside /dev.\n")
        sys.exit(1)

    if not os.path.exists(canonical_path):
        sys.stderr.write(f"Error: Device '{canonical_path}' does not exist.\n")
        sys.exit(1)

    st = os.stat(canonical_path)
    if not stat.S_ISBLK(st.st_mode):
        sys.stderr.write(f"Error: '{canonical_path}' is not a block device.\n")
        sys.exit(1)

    # 2. Must be a whole disk device (sysfs entry must exist in /sys/block/<name>)
    sysfs_block = f"/sys/block/{dev_name}"
    if not os.path.isdir(sysfs_block):
        sys.stderr.write(f"Error: '{dev_name}' is not a whole disk device.\n")
        sys.exit(1)

    # 3. Check critical mount points
    critical_mounts = {"/", "/boot", "/boot/efi", "/home", "/usr", "/var", "/etc"}
    try:
        mounts_info = find_mounts(dev_name)
        for mp in mounts_info.get("mounts", []):
            if mp in critical_mounts:
                sys.stderr.write(
                    f"Error: Refusing to format device containing critical system mount: '{mp}'.\n"
                )
                sys.exit(1)
    except Exception:
        pass

    # 4. Check active swap
    if os.path.exists("/proc/swaps"):
        try:
            with open("/proc/swaps", "r") as f:
                if f"/dev/{dev_name}" in f.read():
                    sys.stderr.write(
                        f"Error: Refusing to format device used as active swap: '/dev/{dev_name}'.\n"
                    )
                    sys.exit(1)
        except OSError:
            pass

    # 5. Verify removable or USB drive
    real_sysfs = os.path.realpath(sysfs_block)
    is_usb = any("usb" in part.lower() for part in real_sysfs.split(os.sep))

    is_removable = False
    removable_file = os.path.join(sysfs_block, "removable")
    if os.path.exists(removable_file):
        try:
            with open(removable_file, "r") as f:
                is_removable = (f.read().strip() == "1")
        except OSError:
            pass

    if not (is_usb or is_removable):
        sys.stderr.write(
            f"Error: Device '{dev_name}' is not recognized as a removable or USB drive.\n"
        )
        sys.exit(1)

    return dev_name


args.device = validate_device_and_label(args.device, args.label)

prefix=""
if "mmcblk" in args.device:
    prefix="p"
elif "nvme" in args.device:
    prefix="p"

# Unmount the drive before writing on it
mounts =  find_mounts(args.device)

for mp in mounts.get("mounts", []):
    if mp and os.path.exists(str(mp)):
        try:
            subprocess.run(["umount", "-f", str(mp)], check=False)
        except Exception:
            pass

for n in mounts.get("names", []):
    if n and os.path.exists(f"/dev/mapper/{n}"):
        try:
            subprocess.run(["dmsetup", "remove", str(n)], check=False)
        except Exception:
            pass

# Erase MBR
with open(f"/dev/{args.device}", "wb") as f:
    f.write(b"\0"*4096)
    f.flush()

subprocess.call(["partprobe", f"/dev/{args.device}"])


# Fill with zeros:
if args.fill:
    writtenBytes = 0
    with open(f"/sys/block/{args.device}/size", "r") as f:
        blockCount = int(f.readline().strip())
    with open(f"/sys/block/{args.device}/queue/logical_block_size", "r") as f:
        blockSize = int(f.readline().strip())
    totalFileBytes = blockCount * blockSize

    writeFile = open(f"/dev/{args.device}", "wb")

    oldMB = 0
    zeros = bytes([0] * blockSize)
    try:
        print("PROGRESS|{}|{}".format(writtenBytes, totalFileBytes))
        sys.stdout.flush()
        while totalFileBytes != writtenBytes:
            if stopWriting:
                break

            writeFile.write(zeros)
            writtenBytes += blockSize

            newMB = int(writtenBytes / 1000 / 1000 / 10)
            if oldMB != newMB:
                oldMB = newMB
                print("PROGRESS|{}|{}".format(writtenBytes, totalFileBytes))
                os.fsync(writeFile)
                sys.stdout.flush()

        writeFile.flush()
    except IOError:
        exit(1)
    else:
        os.fsync(writeFile)
        writeFile.close()

# Make the partition table:
execute(["parted", f"/dev/{args.device}", "mktable", "gpt" if args.gpt else "msdos"])

# Create a partition:
part_type = args.type
if part_type not in ["FAT32", "EXT4", "NTFS", "EXFAT", "BTRFS"]:
    part_type = "EXT4"
execute(["parted", f"/dev/{args.device}", "mkpart", "primary", part_type, "1", "100%"])

# Remove old fs:
execute(["wipefs", "-a", f"/dev/{args.device}{prefix}1", "--force"])

partition = f"/dev/{args.device}{prefix}1"

luks = None
# Crypt:
if args.crypt:
    luks = str(uuid.uuid4())
    create_luks(partition, luks, args.crypt)
    partition = f"/dev/mapper/{luks}"

# Format:
if args.type == "FAT32":
    execute(["mkfs.fat", "-F", "32", "-n", args.label, "-I", partition])
elif args.type == "EXT4":
    execute(["mkfs.ext4", "-L", args.label, partition])
elif args.type == "NTFS":
    execute(["mkfs.ntfs", "-f", "-L", args.label, partition])
elif args.type == "EXFAT":
    execute(["mkfs.exfat", "-L", args.label, partition])
elif args.type == "BTRFS":
    execute(["mkfs.btrfs", "-f", "-L", args.label, partition])


# Close luks
if luks:
    subprocess.call(["cryptsetup", "luksClose", f"/dev/mapper/{luks}"])

# Eject and uneject again to show new partition:
subprocess.call(["eject", f"/dev/{args.device}"])
subprocess.call(["eject", "-t", f"/dev/{args.device}"])

exit(0)
