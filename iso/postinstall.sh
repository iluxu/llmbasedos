#!/usr/bin/env bash
set -eo pipefail

# llmbasedos/iso/postinstall.sh
# Run on the *target system* after base Arch install to configure llmbasedos.

echo "--- Starting LLMBasedOS Post-Installation Configuration ---"

LLMBasedOS_APP_DIR_INSTALLED="/opt/llmbasedos" # Where app code resides on installed system
USERNAME_INSTALLED="llmuser"
USERHOME_INSTALLED="/home/${USERNAME_INSTALLED}"
MCP_GROUP_INSTALLED="llmgroup" # Dedicated group for MCP services and resources

# 1. Create User and Group if they don't exist
if ! getent group "${MCP_GROUP_INSTALLED}" > /dev/null; then
    echo "Creating group ${MCP_GROUP_INSTALLED}..."
    groupadd --system "${MCP_GROUP_INSTALLED}" # System group
else echo "Group ${MCP_GROUP_INSTALLED} already exists."; fi

if ! id "${USERNAME_INSTALLED}" > /dev/null; then
    echo "Creating user ${USERNAME_INSTALLED}..."
    useradd --system -g "${MCP_GROUP_INSTALLED}" -d "${USERHOME_INSTALLED}" -m \
            -s /usr/sbin/nologin -c "LLMBasedOS Service User" "${USERNAME_INSTALLED}"
    # For shell access, change -s /usr/sbin/nologin to /bin/bash and set a password.
    # If luca-shell runs as this user on TTY, shell should be /bin/bash.
    # Let's assume llmuser needs a login shell for luca-shell.
    usermod -s /bin/bash "${USERNAME_INSTALLED}"
    echo "Setting default password for ${USERNAME_INSTALLED}. User MUST change this."
    echo "${USERNAME_INSTALLED}:changeme" | chpasswd # Highly insecure, for demo only!
    echo "User ${USERNAME_INSTALLED} created. Login shell: /bin/bash."
else
    echo "User ${USERNAME_INSTALLED} already exists. Ensuring group membership."
    usermod -aG "${MCP_GROUP_INSTALLED}" "${USERNAME_INSTALLED}"
fi
# Add to tty group for TTY access by luca-shell, and docker group for agent server
usermod -aG tty "${USERNAME_INSTALLED}"
if pacman -Q docker &>/dev/null; then # If docker is installed
    usermod -aG docker "${USERNAME_INSTALLED}"
    echo "Added ${USERNAME_INSTALLED} to docker group. Re-login required for effect."
fi


# 2. Permissions for application and configuration directories
echo "Setting permissions for application and config directories..."
if [ -d "${LLMBasedOS_APP_DIR_INSTALLED}" ]; then
    chown -R "${USERNAME_INSTALLED}:${MCP_GROUP_INSTALLED}" "${LLMBasedOS_APP_DIR_INSTALLED}"
    find "${LLMBasedOS_APP_DIR_INSTALLED}" -type d -exec chmod 750 {} \; # u=rwx, g=rx, o=
    find "${LLMBasedOS_APP_DIR_INSTALLED}" -type f -exec chmod 640 {} \; # u=rw, g=r, o=
    find "${LLMBasedOS_APP_DIR_INSTALLED}" -type f -name "*.sh" -exec chmod 750 {} \; # Executable scripts
else echo "Warning: ${LLMBasedOS_APP_DIR_INSTALLED} not found. Skipping permissions."; fi

mkdir -p /etc/llmbasedos/workflows
chown -R "${USERNAME_INSTALLED}:${MCP_GROUP_INSTALLED}" /etc/llmbasedos
chmod 770 /etc/llmbasedos # Dir: u=rwx, g=rwx (for config file updates by services if needed)
chmod 750 /etc/llmbasedos/workflows # u=rwx, g=rx
# Config files within /etc/llmbasedos should be u=rw, g=r (640)
find /etc/llmbasedos -type f -exec chmod 640 {} \;
# Ensure lic.key has strict permissions if it contains sensitive data
if [ -f "/etc/llmbasedos/lic.key" ]; then chmod 640 "/etc/llmbasedos/lic.key"; fi


# 3. /run/mcp for sockets
MCP_RUN_DIR_INSTALLED="/run/mcp" # systemd's RuntimeDirectory= in service files is better
# If not using RuntimeDirectory=, create and set perms here.
# mkdir -p "${MCP_RUN_DIR_INSTALLED}"
# chown "${USERNAME_INSTALLED}:${MCP_GROUP_INSTALLED}" "${MCP_RUN_DIR_INSTALLED}"
# chmod 2770 "${MCP_RUN_DIR_INSTALLED}" # rwxrws--- (setgid bit so sockets inherit group)
# For systemd >v247, RuntimeDirectoryDefaultMode=0755, and RuntimeDirectoryPreserve=
# Sockets created by services running as llmuser:llmgroup will have that ownership.
# Socket permissions (0660) are set by MCPServer.start().
# Ensure parent /run/mcp is writable by llmuser or llmgroup if RuntimeDirectory not used.
# Best: define RuntimeDirectory=mcp in service files, systemd handles creation & perms.
# Let's assume service files will handle /run/mcp creation.

# 4. Log and lib directories
LOG_DIR_INSTALLED="/var/log/llmbasedos"
LIB_DIR_INSTALLED="/var/lib/llmbasedos" # For FAISS index, etc.
mkdir -p "${LOG_DIR_INSTALLED}" "${LIB_DIR_INSTALLED}"
chown -R "${USERNAME_INSTALLED}:${MCP_GROUP_INSTALLED}" "${LOG_DIR_INSTALLED}" "${LIB_DIR_INSTALLED}"
chmod -R 770 "${LOG_DIR_INSTALLED}" # u=rwx, g=rwx for log writing
chmod -R 770 "${LIB_DIR_INSTALLED}" # u=rwx, g=rwx for lib data (FAISS index)

# 5. Enable Systemd Services
echo "Enabling and starting systemd services for llmbasedos..."
systemctl daemon-reload # Refresh systemd manager configuration

SERVICES_TO_ENABLE_INSTALLED=(
    "mcp-gateway.service" "mcp-fs.service" "mcp-sync.service"
    "mcp-mail.service" "mcp-agent.service"
)
# Also enable NetworkManager, sshd, docker if they were chosen during install
if pacman -Q networkmanager &>/dev/null; then systemctl enable NetworkManager.service; fi
if pacman -Q openssh &>/dev/null; then systemctl enable sshd.service; fi
if pacman -Q docker &>/dev/null; then systemctl enable docker.service; fi


for service_inst in "\${SERVICES_TO_ENABLE_INSTALLED[@]}"; do
    if systemctl list-unit-files | grep -q "^\${service_inst}"; then
        echo "Enabling \${service_inst}..."
        systemctl enable "\${service_inst}"
        # Optionally start them now, or let reboot handle it
        # systemctl start "\${service_inst}" || echo "Warning: Failed to start \${service_inst} immediately."
    else echo "Warning: Service file \${service_inst} not found. Cannot enable."; fi
done

# 6. Configure TTY1 for luca-shell (for installed system)
echo "Configuring TTY1 for luca-shell..."
if systemctl list-unit-files | grep -q "^luca-shell@.service"; then
    if systemctl list-unit-files | grep -q "^getty@tty1.service"; then
        echo "Disabling getty@tty1.service..."
        systemctl disable getty@tty1.service # Standard getty on TTY1
        echo "Masking getty@tty1.service to prevent it from being started by getty.target..."
        systemctl mask getty@tty1.service # Prevent getty.target from pulling it in
    fi
    echo "Enabling luca-shell@tty1.service..."
    systemctl enable luca-shell@tty1.service # Our custom shell service for TTY1
else echo "Warning: luca-shell@.service not found. TTY1 setup skipped."; fi

# 7. Final messages
echo "--- LLMBasedOS Post-Installation Complete ---"
echo "User '${USERNAME_INSTALLED}' created/configured (password: changeme - CHANGE IMMEDIATELY!)."
echo "Services enabled. A reboot is recommended for all changes to take full effect."
echo "After reboot, luca-shell should be on TTY1, and MCP gateway accessible."
echo "Remember to configure API keys in /etc/llmbasedos/gateway.env (or similar) if needed."
