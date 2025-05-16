#!/usr/bin/env bash

set -eo pipefail # Exit on error, treat unset variables as an error, and propagate exit status

# --- Configuration ---
PROFILE_DIR_ABS="$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)" # Absolute path to this script's dir (iso/)
WORK_DIR_ABS="${PROFILE_DIR_ABS}/work"   # Archiso working directory
OUT_DIR_ABS="${PROFILE_DIR_ABS}/out"    # Output directory for ISO
REPO_ROOT_ABS="$(cd "${PROFILE_DIR_ABS}/../.." &>/dev/null && pwd)" # Root of llmbasedos repo

LLMBasedOS_APP_TARGET_DIR_NAME="llmbasedos" # Name of the app dir inside /opt

# Verbosity for mkarchiso (-v)
MKARCHISO_OPTS="-v" # Add other mkarchiso options if needed

# Clean up previous build
cleanup() {
    echo "--- Cleaning up work directory: ${WORK_DIR_ABS} ---"
    if [ -d "${WORK_DIR_ABS}" ]; then
        if [[ "${WORK_DIR_ABS}" == *"/iso/work" ]]; then # Safety check path
            sudo rm -rf "${WORK_DIR_ABS}"
            echo "Work directory cleaned."
        else
            echo "Error: WORK_DIR_ABS path '${WORK_DIR_ABS}' seems unsafe. Aborting cleanup." >&2; exit 1;
        fi
    fi
}

# Prepare the airootfs for customization
prepare_airootfs_customizations() {
    local airootfs_mnt_path="${1}" # Path to the mounted airootfs (e.g., ${WORK_DIR_ABS}/x86_64/airootfs)
    echo "--- Preparing airootfs customizations in ${airootfs_mnt_path} ---"

    # 1. Copy the llmbasedos application repository
    local app_target_path="${airootfs_mnt_path}/opt/${LLMBasedOS_APP_TARGET_DIR_NAME}"
    echo "Copying llmbasedos repository to ${app_target_path}..."
    mkdir -p "${app_target_path}"
    rsync -a --delete --checksum \
        --exclude ".git" --exclude ".github" --exclude "iso/work" --exclude "iso/out" \
        --exclude "*.pyc" --exclude "__pycache__" --exclude ".DS_Store" \
        --exclude "venv" --exclude ".venv" --exclude "docs/_build" \
        "${REPO_ROOT_ABS}/" "${app_target_path}/"

    # 2. Install Python dependencies (system-wide pip install)
    echo "Installing Python dependencies system-wide via pip..."
    PIP_REQ_FILES=(
        "${app_target_path}/gateway/requirements.txt"
        "${app_target_path}/servers/fs/requirements.txt"
        "${app_target_path}/servers/sync/requirements.txt"
        "${app_target_path}/servers/mail/requirements.txt"
        "${app_target_path}/servers/agent/requirements.txt"
        "${app_target_path}/shell/requirements.txt"
    )
    for req_file in "${PIP_REQ_FILES[@]}"; do
        if [ -f "${req_file}" ]; then
            echo "Installing from ${req_file}..."
            # Using --break-system-packages if pip version is new and system python
            # Or, better, ensure python-pip is from system and compatible.
            # Or, use a venv within /opt/llmbasedos_env and adjust ExecStart in services.
            # For simplicity in minimal OS, system-wide for now.
            sudo arch-chroot "${airootfs_mnt_path}" pip install --no-cache-dir -r "${req_file}"
        else
            echo "Warning: Requirements file not found: ${req_file}"
        fi
    done
    echo "Cleaning up pip cache inside chroot..."
    sudo arch-chroot "${airootfs_mnt_path}" rm -rf /root/.cache/pip # Pip cache for root user

    # 3. Copy systemd service units
    local systemd_units_source_dir_iso="${PROFILE_DIR_ABS}/systemd_units" # Units stored in iso/systemd_units/
    local systemd_units_target_dir_airootfs="${airootfs_mnt_path}/etc/systemd/system"
    echo "Copying systemd service units to ${systemd_units_target_dir_airootfs}..."
    mkdir -p "${systemd_units_target_dir_airootfs}"
    if [ -d "${systemd_units_source_dir_iso}" ]; then
        sudo cp -v "${systemd_units_source_dir_iso}/"*.service "${systemd_units_target_dir_airootfs}/"
        sudo chmod 644 "${systemd_units_target_dir_airootfs}/"*.service # Ensure correct permissions
    else
        echo "Warning: Systemd units source directory ${systemd_units_source_dir_iso} not found."
    fi

    # 4. Copy post-installation script
    local postinstall_script_target_path="${airootfs_mnt_path}/root/llmbasedos_postinstall.sh" # Place in root for installer
    echo "Copying postinstall.sh to ${postinstall_script_target_path}..."
    sudo cp "${PROFILE_DIR_ABS}/postinstall.sh" "${postinstall_script_target_path}"
    sudo chmod 755 "${postinstall_script_target_path}"

    # 5. Create dummy/default configuration files and directories
    echo "Creating default configuration files/directories..."
    sudo mkdir -p "${airootfs_mnt_path}/etc/llmbasedos"
    sudo mkdir -p "${airootfs_mnt_path}/etc/llmbasedos/workflows" # For agent
    # Dummy licence key (FREE tier)
    if [ ! -f "${airootfs_mnt_path}/etc/llmbasedos/lic.key" ]; then
        sudo tee "${airootfs_mnt_path}/etc/llmbasedos/lic.key" > /dev/null <<LIC_EOF
# Default FREE tier licence. Replace with a valid key.
tier: FREE
user_id: default_live_user
expires_at: "$(date -d "+1 year" +%Y-%m-%dT%H:%M:%SZ)" # Example expiry
LIC_EOF
        sudo chmod 640 "${airootfs_mnt_path}/etc/llmbasedos/lic.key" # Readable by llmgroup
    fi
    # Dummy licence tiers config (gateway will use defaults if this is missing/empty)
    if [ ! -f "${airootfs_mnt_path}/etc/llmbasedos/licence_tiers.yaml" ]; then
        sudo tee "${airootfs_mnt_path}/etc/llmbasedos/licence_tiers.yaml" > /dev/null <<TIERS_EOF
# Empty: Gateway will use its internal DEFAULT_LICENCE_TIERS.
# Add custom tier definitions here if needed.
TIERS_EOF
        sudo chmod 644 "${airootfs_mnt_path}/etc/llmbasedos/licence_tiers.yaml"
    fi
    # Dummy mail accounts config
    if [ ! -f "${airootfs_mnt_path}/etc/llmbasedos/mail_accounts.yaml" ]; then
        sudo tee "${airootfs_mnt_path}/etc/llmbasedos/mail_accounts.yaml" > /dev/null <<MAIL_EOF
# Example mail account configuration (replace with real details)
# my_gmail_account:
#   email: "myemail@gmail.com"
#   host: "imap.gmail.com"
#   port: 993
#   user: "myemail@gmail.com"
#   password: "YOUR_APP_PASSWORD_HERE" # Use App Password for Gmail
#   ssl: true
MAIL_EOF
        sudo chmod 640 "${airootfs_mnt_path}/etc/llmbasedos/mail_accounts.yaml" # Readable by llmgroup
    fi

    # 6. Set hostname for the live ISO environment
    echo "Setting hostname for live system..."
    sudo tee "${airootfs_mnt_path}/etc/hostname" > /dev/null <<< "llmbasedos-live"

    # 7. Create user/group and enable services for the LIVE ISO environment
    # This is separate from postinstall.sh which is for the INSTALLED system.
    echo "Configuring live environment user and services..."
    # Create user llmuser and group llmgroup inside chroot
    sudo arch-chroot "${airootfs_mnt_path}" groupadd llmgroup --gid 1001 || echo "Group llmgroup may already exist."
    sudo arch-chroot "${airootfs_mnt_path}" useradd llmuser --uid 1001 --gid llmgroup -m -s /bin/bash -c "LLMBasedOS Service User" --groups wheel,tty,docker || echo "User llmuser may already exist."
    # Set a default password for live user (optional, can also use autologin)
    echo 'llmuser:livepass' | sudo arch-chroot "${airootfs_mnt_path}" chpasswd
    echo "Created live user 'llmuser' (pass: livepass) and group 'llmgroup'."
    # Add llmuser to docker group if docker package was installed
    if sudo arch-chroot "${airootfs_mnt_path}" pacman -Q docker &>/dev/null; then
      sudo arch-chroot "${airootfs_mnt_path}" usermod -aG docker llmuser
      echo "Added llmuser to docker group for live environment."
    fi


    # Enable services for multi-user.target in live environment
    # This creates symlinks in /etc/systemd/system/multi-user.target.wants/
    LIVE_SERVICES_TO_ENABLE=(
        "NetworkManager.service" # Essential for network
        "sshd.service"           # Optional for remote access
        "docker.service"         # If docker is used by agent server
        "mcp-gateway.service"
        "mcp-fs.service"
        "mcp-sync.service"
        "mcp-mail.service"
        "mcp-agent.service"
    )
    for service_live in "${LIVE_SERVICES_TO_ENABLE[@]}"; do
        if [ -f "${airootfs_mnt_path}/etc/systemd/system/${service_live}" ] || \
           [ -f "${airootfs_mnt_path}/usr/lib/systemd/system/${service_live}" ]; then
            echo "Enabling ${service_live} for live environment..."
            sudo arch-chroot "${airootfs_mnt_path}" systemctl enable "${service_live}"
        else
            echo "Warning: Service ${service_live} not found, cannot enable for live env."
        fi
    done

    # Configure TTY1 for luca-shell in live environment (autologin as llmuser)
    # This uses a drop-in for getty@tty1 to autologin llmuser,
    # then llmuser's .bash_profile/.zprofile should start luca-shell.
    echo "Configuring TTY1 for autologin and luca-shell in live environment..."
    local getty_autologin_dir="${airootfs_mnt_path}/etc/systemd/system/getty@tty1.service.d"
    sudo mkdir -p "${getty_autologin_dir}"
    sudo tee "${getty_autologin_dir}/autologin.conf" > /dev/null << AUTOLOGIN_EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin llmuser --noclear %I \$TERM
AUTOLOGIN_EOF
    # Add luca-shell startup to llmuser's .bash_profile
    local llmuser_bash_profile="${airootfs_mnt_path}/home/llmuser/.bash_profile"
    sudo tee -a "${llmuser_bash_profile}" > /dev/null << PROFILE_EOF

# Start luca-shell if on TTY1 and no X server
if [ "\$(tty)" = "/dev/tty1" ] && [ -z "\$DISPLAY" ]; then
    echo "Starting luca-shell on TTY1..."
    # Ensure PATH includes /usr/local/bin if python is there, or use /usr/bin/python
    # Make sure PYTHONPATH is set if llmbasedos is not installed in standard site-packages
    export PYTHONPATH=/opt # Crucial for finding llmbasedos package
    exec /usr/bin/python -m llmbasedos.shell.luca
fi
PROFILE_EOF
    sudo chown 1001:1001 "${llmuser_bash_profile}" # llmuser:llmgroup
    sudo chmod 644 "${llmuser_bash_profile}"
    
    # Ensure /run/mcp and log/lib dirs exist with correct perms for live boot
    sudo mkdir -p "${airootfs_mnt_path}/run/mcp"
    sudo chown 1001:1001 "${airootfs_mnt_path}/run/mcp" # llmuser:llmgroup
    sudo chmod 1770 "${airootfs_mnt_path}/run/mcp" # Sticky, rwxrwx---

    sudo mkdir -p "${airootfs_mnt_path}/var/log/llmbasedos"
    sudo chown 1001:1001 "${airootfs_mnt_path}/var/log/llmbasedos"
    sudo chmod 770 "${airootfs_mnt_path}/var/log/llmbasedos"

    sudo mkdir -p "${airootfs_mnt_path}/var/lib/llmbasedos"
    sudo chown 1001:1001 "${airootfs_mnt_path}/var/lib/llmbasedos"
    sudo chmod 770 "${airootfs_mnt_path}/var/lib/llmbasedos"


    echo "Airootfs customizations complete."
}

# --- Main Build Process ---
trap cleanup EXIT ERR INT TERM # Cleanup on exit or error

# Check for root/sudo privileges (mkarchiso needs it)
if [ "$(id -u)" -ne 0 ]; then
    echo "This script uses 'sudo' for mkarchiso and chroot. Ensure sudo is configured."
    # No exit needed, sudo will prompt if required.
fi

# Ensure mkarchiso is installed
if ! command -v mkarchiso &> /dev/null; then
    echo "mkarchiso command not found. Please install 'archiso' package." >&2; exit 1;
fi

echo "--- Starting llmbasedos ISO build process ---"
# 1. Initial cleanup and directory creation
cleanup
mkdir -p "${WORK_DIR_ABS}" "${OUT_DIR_ABS}"

# 2. Prepare profile directory for mkarchiso (copy essential configs)
#    profiledef.sh is already in ${PROFILE_DIR_ABS}
#    Copy pacman.conf and mirrorlist to where mkarchiso expects them for the profile.
echo "Copying pacman.conf and mirrorlist to profile directory..."
cp "${PROFILE_DIR_ABS}/pacman.conf" "${PROFILE_DIR_ABS}/pacman.conf" # It's already there, just ensures it's named correctly
cp "${PROFILE_DIR_ABS}/mirrorlist-llmbasedos-build" "${PROFILE_DIR_ABS}/pacman.d/mirrorlist" # mkarchiso looks in pacman.d/ relative to profile dir
                                                                                            # Or, /etc/pacman.d/mirrorlist inside chroot if pacman.conf uses that.
                                                                                            # The profiledef.sh pacman_conf points to ${PWD}/pacman.conf.
                                                                                            # This pacman.conf includes /etc/pacman.d/mirrorlist-llmbasedos-build
                                                                                            # So, we need to place it there inside the chroot structure later.
                                                                                            # Or, simpler: make profiledef.sh's pacman.conf use a path relative to profile dir.
                                                                                            # The `${PWD}` in profiledef.sh refers to the workdir when mkarchiso runs.
                                                                                            # Let's ensure it's copied to workdir too.
# Simpler way for mirrorlist with `pacman_conf` in `profiledef.sh`:
# The `pacman.conf` in `profiledef.sh` should point to a mirrorlist file that will exist *inside the chroot*.
# So, copy our build mirrorlist to the location inside airootfs that pacman.conf will reference.
# We'll do this during airootfs customization.

# 3. Build the ISO using mkarchiso.
#    mkarchiso will:
#    a. Create a base chroot environment using pacstrap (using pkg_list from profiledef.sh).
#    b. Execute customization hooks if defined (we do it manually after base chroot is made).
#    c. Package everything into an ISO.

# Using mkarchiso's `-C <config_dir_override_for_airootfs_copy_commands>` could be an option
# or `-r <run_script_in_chroot_after_pacstrap>` for some customizations.
# Standard flow:
#  mkarchiso -w work -o out -v prepare ... (does pacstrap, makes airootfs base)
#  (manual customization of work/arch/airootfs)
#  mkarchiso -w work -o out -v build ... (takes the customized airootfs and makes ISO)
# Or, just `mkarchiso -w work -o out -v profile_dir` and it does all if hooks are set up.
# Let's use a two-step approach for clarity: prepare, customize, then build.

echo "Running mkarchiso prepare step (pacstrap)..."
# This command structure might vary based on exact mkarchiso version and desired control.
# The `-p` option is not standard for "prepare only".
# A common way is to use a minimal profile and then customize.
# For full control, use arch-install-scripts' pacstrap directly if needed,
# or structure `profiledef.sh` with `init_airootfs` and `customize_airootfs` functions.

# Let's assume `mkarchiso ... <profile_dir>` does a full build, and we need to hook customizations.
# Archiso's `customize_airootfs.sh` script (if placed in profile dir) is run after pacstrap.
# So, let's put our `prepare_airootfs_customizations` logic into such a script.

# Create customize_airootfs.sh that mkarchiso will run
cat > "${PROFILE_DIR_ABS}/customize_airootfs.sh" << CUSTOMIZE_SCRIPT_EOF
#!/usr/bin/env bash
set -eo pipefail
echo "--- Running customize_airootfs.sh from mkarchiso hook ---"

# The script needs access to functions and variables from build.sh
# This is tricky. Let's redefine or source.
# For simplicity, redefine the core logic here or make build.sh functions exportable.
# Easier: Call prepare_airootfs_customizations directly with '/' as airootfs_mnt_path,
# because this script runs *inside* the chroot.

# Path to the app code, now relative to chroot's /
APP_SOURCE_ON_HOST_FOR_COPY="/ PaisibleAI_Outside_Chroot_Repo_Path_Placeholder /llmbasedos" # This path needs to be accessible from chroot or copied earlier by mkarchiso's generic copy
                                                              # mkarchiso copies contents of profile dir/airootfs/ into chroot /
                                                              # So, if llmbasedos repo is in profile_dir/airootfs_overlay/opt/llmbasedos, it's copied to /opt/llmbasedos
                                                              # Let's assume mkarchiso's copy mechanism handles this via airootfs directory in profile.

# We need to make `build.sh` place the repo into `iso/airootfs/opt/llmbasedos`
# so mkarchiso copies it into the chroot's `/opt/llmbasedos`.
# Then this script can work on `/opt/llmbasedos`.

# Call the customization function (ensure it's defined or sourced if in separate file)
# For this script, the function is not available. We need to inline its logic or source.
# Let's inline the core logic that needs to run *inside* the chroot.

echo "Customizing airootfs from within chroot..."

# Paths are now relative to chroot's /
CHROOT_APP_DIR="/opt/${LLMBasedOS_APP_TARGET_DIR_NAME}" # Should match build.sh's var

echo "Installing Python dependencies system-wide via pip (from chroot)..."
PIP_REQ_FILES_CHROOT=(
    "\${CHROOT_APP_DIR}/gateway/requirements.txt"
    "\${CHROOT_APP_DIR}/servers/fs/requirements.txt"
    "\${CHROOT_APP_DIR}/servers/sync/requirements.txt"
    "\${CHROOT_APP_DIR}/servers/mail/requirements.txt"
    "\${CHROOT_APP_DIR}/servers/agent/requirements.txt"
    "\${CHROOT_APP_DIR}/shell/requirements.txt"
)
for req_file_chroot in "\${PIP_REQ_FILES_CHROOT[@]}"; do
    if [ -f "\${req_file_chroot}" ]; then
        echo "Installing from \${req_file_chroot}..."
        pip install --no-cache-dir -r "\${req_file_chroot}"
    else
        echo "Warning (chroot): Requirements file not found: \${req_file_chroot}"
    fi
done
echo "Cleaning up pip cache (from chroot)..."
rm -rf /root/.cache/pip

echo "Copying systemd service units (from chroot perspective)..."
# Units are already copied to /etc/systemd/system by mkarchiso if they were in profile_dir/airootfs/etc/systemd/system
# Or, if they are in profile_dir/systemd_units, we copy them here if this script is run by -r option
# Assuming units are in CHROOT_APP_DIR/iso/systemd_units and need to be copied/linked
SYSTEMD_UNITS_SOURCE_IN_CHROOT="\${CHROOT_APP_DIR}/iso/systemd_units"
SYSTEMD_TARGET_DIR="/etc/systemd/system"
if [ -d "\${SYSTEMD_UNITS_SOURCE_IN_CHROOT}" ]; then
    cp -v "\${SYSTEMD_UNITS_SOURCE_IN_CHROOT}/"*.service "\${SYSTEMD_TARGET_DIR}/"
    chmod 644 "\${SYSTEMD_TARGET_DIR}/"*.service
else
    echo "Warning (chroot): Systemd units source \${SYSTEMD_UNITS_SOURCE_IN_CHROOT} not found."
fi

echo "Creating default configuration files/directories (from chroot)..."
mkdir -p /etc/llmbasedos/workflows
# Dummy licence key
if [ ! -f "/etc/llmbasedos/lic.key" ]; then
    tee "/etc/llmbasedos/lic.key" > /dev/null <<LIC_EOF_CHROOT
tier: FREE
user_id: default_chroot_user
expires_at: "$(date -d "+1 year" +%Y-%m-%dT%H:%M:%SZ)"
LIC_EOF_CHROOT
    chmod 640 "/etc/llmbasedos/lic.key"
fi
# Other dummy configs (tiers, mail) similar to prepare_airootfs_customizations

echo "Setting hostname (from chroot)..."
tee "/etc/hostname" > /dev/null <<< "llmbasedos-live"

echo "Configuring live environment user and services (from chroot)..."
groupadd llmgroup --gid 1001 || echo "Group llmgroup may already exist."
useradd llmuser --uid 1001 --gid llmgroup -m -s /bin/bash -c "LLMBasedOS Service User" --groups wheel,tty,docker || echo "User llmuser may already exist."
echo 'llmuser:livepass' | chpasswd
echo "Created live user 'llmuser' (pass: livepass)."
if pacman -Q docker &>/dev/null; then usermod -aG docker llmuser; fi

LIVE_SERVICES_TO_ENABLE_CHROOT=(
    "NetworkManager.service" "sshd.service" "docker.service"
    "mcp-gateway.service" "mcp-fs.service" "mcp-sync.service"
    "mcp-mail.service" "mcp-agent.service"
)
for service_chroot in "\${LIVE_SERVICES_TO_ENABLE_CHROOT[@]}"; do
    if [ -f "/etc/systemd/system/\${service_chroot}" ] || [ -f "/usr/lib/systemd/system/\${service_chroot}" ]; then
        echo "Enabling \${service_chroot} for live env (chroot)..."
        systemctl enable "\${service_chroot}"
    else echo "Warning (chroot): Service \${service_chroot} not found."; fi
done

echo "Configuring TTY1 autologin for llmuser and luca-shell (from chroot)..."
GETTY_AUTOLOGIN_DIR="/etc/systemd/system/getty@tty1.service.d"
mkdir -p "\${GETTY_AUTOLOGIN_DIR}"
tee "\${GETTY_AUTOLOGIN_DIR}/autologin.conf" > /dev/null << AUTOLOGIN_EOF_CHROOT
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin llmuser --noclear %I \$TERM
AUTOLOGIN_EOF_CHROOT

LLMUSER_BASH_PROFILE="/home/llmuser/.bash_profile"
tee -a "\${LLMUSER_BASH_PROFILE}" > /dev/null << PROFILE_EOF_CHROOT
if [ "\\\$(tty)" = "/dev/tty1" ] && [ -z "\\\$DISPLAY" ]; then
    echo "Starting luca-shell on TTY1 (from .bash_profile)..."
    export PYTHONPATH=/opt # Crucial
    exec /usr/bin/python -m llmbasedos.shell.luca
fi
PROFILE_EOF_CHROOT
chown 1001:1001 "\${LLMUSER_BASH_PROFILE}"; chmod 644 "\${LLMUSER_BASH_PROFILE}"

# Ensure /run/mcp and log/lib dirs exist with correct perms for live boot
mkdir -p /run/mcp; chown 1001:1001 /run/mcp; chmod 1770 /run/mcp
mkdir -p /var/log/llmbasedos; chown 1001:1001 /var/log/llmbasedos; chmod 770 /var/log/llmbasedos
mkdir -p /var/lib/llmbasedos; chown 1001:1001 /var/lib/llmbasedos; chmod 770 /var/lib/llmbasedos

# Copy postinstall script to /root for installer access
cp "\${CHROOT_APP_DIR}/iso/postinstall.sh" "/root/llmbasedos_postinstall.sh"
chmod 755 "/root/llmbasedos_postinstall.sh"

echo "--- customize_airootfs.sh COMPLETED ---"
CUSTOMIZE_SCRIPT_EOF
chmod +x "${PROFILE_DIR_ABS}/customize_airootfs.sh"

# Now, prepare the airootfs overlay that mkarchiso will use.
# mkarchiso copies contents of `profile_dir/airootfs/` into the chroot's `/`.
AIROOTFS_OVERLAY_DIR="${PROFILE_DIR_ABS}/airootfs"
mkdir -p "${AIROOTFS_OVERLAY_DIR}/opt/${LLMBasedOS_APP_TARGET_DIR_NAME}"
mkdir -p "${AIROOTFS_OVERLAY_DIR}/etc/pacman.d" # For mirrorlist

echo "Copying application code to airootfs overlay for mkarchiso..."
rsync -a --delete --checksum \
    --exclude ".git" --exclude ".github" --exclude "iso/work" --exclude "iso/out" \
    --exclude "*.pyc" --exclude "__pycache__" --exclude ".DS_Store" \
    --exclude "venv" --exclude ".venv" --exclude "docs/_build" \
    "${REPO_ROOT_ABS}/" "${AIROOTFS_OVERLAY_DIR}/opt/${LLMBasedOS_APP_TARGET_DIR_NAME}/"

echo "Copying build-time mirrorlist for use inside chroot by pacman..."
cp "${PROFILE_DIR_ABS}/mirrorlist-llmbasedos-build" "${AIROOTFS_OVERLAY_DIR}/etc/pacman.d/mirrorlist-llmbasedos-build"


# Finally, run mkarchiso. It will use profiledef.sh and execute customize_airootfs.sh.
echo "Building the ISO image with mkarchiso..."
# The profile directory is PROFILE_DIR_ABS
sudo mkarchiso ${MKARCHISO_OPTS} -w "${WORK_DIR_ABS}" -o "${OUT_DIR_ABS}" "${PROFILE_DIR_ABS}"

FINAL_ISO_PATH_FOUND=$(find "${OUT_DIR_ABS}" -name "llmbasedos*.iso" -print -quit)

if [ -f "${FINAL_ISO_PATH_FOUND}" ]; then
    echo "--- ISO Build Successful! ---"
    echo "ISO created at: ${FINAL_ISO_PATH_FOUND}"
    ls -lh "${FINAL_ISO_PATH_FOUND}"
else
    echo "--- ISO Build Failed. Check logs in ${WORK_DIR_ABS}. ---" >&2; exit 1;
fi

# Cleanup customize_airootfs.sh and airootfs overlay dir from profile after build
rm -f "${PROFILE_DIR_ABS}/customize_airootfs.sh"
# sudo rm -rf "${AIROOTFS_OVERLAY_DIR}" # Optional: clean up overlay

exit 0
