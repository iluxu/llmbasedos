#!/usr/bin/env bash
# shellcheck disable=SC2034

# llmbasedos/iso/profiledef.sh (Archiso profile definition)

iso_name="llmbasedos"
iso_label="LLMBasedOS_$(date +%Y%m)"
iso_publisher="llmbasedos Project <your_email_or_site>" # Customize
iso_application="LLMBasedOS Minimal Linux with MCP"
iso_version="$(date +%Y.%m.%d)"
install_dir="arch" 
buildmodes=('iso')
bootmodes=('bios.syslinux.mbr' 'bios.syslinux.eltorito'
           'uefi-x64.systemd-boot.esp' 'uefi-x64.systemd-boot.eltorito')
arch="x86_64"
pacman_conf="${PWD}/pacman.conf" # Use pacman.conf from this profile directory
airootfs_image_type="squashfs"
airootfs_image_tool_options=('-comp' 'xz' '-Xbcj' 'x86' '-b' '1M' '-noappend') # Standard compression

# File permissions (adjust as needed, especially for /etc/llmbasedos files)
file_permissions=(
  ["/etc/shadow"]="0:0:400"
  ["/root"]="0:0:750"
  ["/etc/sudoers.d"]="0:0:755"
  ["/opt/llmbasedos"]="0:0:755" # Base app dir
  ["/opt/llmbasedos/**/*.py"]="0:0:644"
  ["/opt/llmbasedos/**/*.sh"]="0:0:755"
  ["/etc/llmbasedos"]="0:0:750" # Root owned, llmuser/llmgroup readable (or more restrictive)
  ["/etc/llmbasedos/lic.key"]="0:42:640" # Example: root:shadow readable by shadow group (if sensitive)
                                       # Or root:llmgroup 640 if llmuser/llmgroup needs to read it
                                       # For now, assume gateway (llmuser) reads it, so 0:llmgroup 640
  ["/etc/llmbasedos/mail_accounts.yaml"]="0:llmgroup:640" # Readable by llmgroup
  ["/etc/llmbasedos/workflows"]="0:llmgroup:750" # Workflows dir
  ["/etc/llmbasedos/licence_tiers.yaml"]="0:0:644" # Globally readable for tiers info
  ["/etc/systemd/system/*"]="0:0:644"
  ["/run/mcp"]="llmuser:llmgroup:1770" # Sticky bit, owned by llmuser:llmgroup, rwxrwx--T
                                      # Services create their own sockets here.
  ["/var/log/llmbasedos"]="llmuser:llmgroup:770" # Writable by services
  ["/var/lib/llmbasedos"]="llmuser:llmgroup:770" # For FAISS index etc.
)

# System packages needed for llmbasedos and its dependencies
# Python dependencies will be installed via pip in build.sh's airootfs customization
pkg_list=(
    "base" "linux" "linux-firmware" "systemd" "systemd-sysvcompat" "mkinitcpio" "pacman"
    "sudo" "networkmanager" "openssh" "git" # Core utilities
    "python" "python-pip" # Python runtime and pip
    # System libraries needed by Python packages (examples, verify specific needs)
    "gcc" "make" "rust" # For compiling some Python packages if installed from source by pip
    "blas" "lapack" # For numpy/scipy often needed by ML libs
    "tk" # Sometimes needed by matplotlib, indirectly by sentence-transformers vis
    # llmbasedos direct system dependencies
    "rclone"                            # For sync server
    "docker"                            # For agent server (Docker workflows)
    "libmagic"                          # For python-magic (fs server)
    "faiss-cpu"                         # If FAISS is from system repo (ensure name matches)
    # Other useful tools for a minimal system
    "vim" "curl" "wget" "htop" "man-db" "man-pages" "less" "tree" "tmux"
    "archiso" # If building on the ISO itself, for dev
)

# Remove unnecessary files from ISO (customize heavily for minimal size)
rm_iso=(
    "usr/lib/systemd/system/multi-user.target.wants/{graphical.target,plymouth*,rescue*,emergency*}"
    "etc/systemd/system/*.wants/{plymouth*,rescue*,emergency*}"
    "var/log/journal/*" "var/cache/pacman/pkg/*"
    "usr/share/man/*" "!usr/share/man/man1" "!usr/share/man/man5" "!usr/share/man/man8" # Keep some man pages
    "usr/share/doc/*" "!usr/share/doc/llmbasedos*" # Keep our own docs if any
    "usr/share/locale/*" "!usr/share/locale/en_US" "!usr/share/locale/locale.alias" # Keep English only
    "usr/include" # Development headers (unless building on ISO)
    "usr/lib/python*/test" "usr/lib/python*/unittest" # Python test files
    "usr/lib/python*/site-packages/pip*" # Pip itself after installs (optional)
    "usr/lib/python*/site-packages/setuptools*" # Setuptools after installs (optional)
    "usr/lib/python*/site-packages/wheel*" # Wheel after installs (optional)
    "opt/llmbasedos/**/*.pyc" "opt/llmbasedos/**/__pycache__" # Clean Python bytecode
)
