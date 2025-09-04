#!/usr/bin/env bash

# --- VesselKeeper Installation Script ---

set -e # Exit immediately if a command exits with a non-zero status.

# --- Configuration ---
# Define application details
APP_NAME="VesselKeeper"
APP_EXEC_NAME="vesselkeeper" # Name for the installed executable/script
APP_SCRIPT="VESSEL-KEEPER-V1" # Your current script name
APP_DESKTOP_FILE="${APP_EXEC_NAME}.desktop"
APP_ICON_FILE="vesselkeeper-icon.png" # Change if your icon has a different name/ext

# Default installation directory (can be overridden by user input)
DEFAULT_INSTALL_DIR="${HOME}/.local/share/${APP_EXEC_NAME}"

# Log file for the installer (placed in install dir)
LOG_FILE="" # Will be set after user provides install dir

# --- Functions ---

log() {
    if [[ -n "$LOG_FILE" && -f "$(dirname "$LOG_FILE")" ]]; then
        echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
    else
        echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1"
    fi
}

prompt_for_install_dir() {
    local input_dir
    while true; do
        echo
        read -e -p "Enter installation directory (or press Enter for default: $DEFAULT_INSTALL_DIR): " input_dir
        input_dir=$(eval echo "${input_dir:-$DEFAULT_INSTALL_DIR}") # Expand ~ if present

        if [[ -z "$input_dir" ]]; then
            input_dir="$DEFAULT_INSTALL_DIR"
        fi

        # Resolve to absolute path
        input_dir=$(realpath -m "$input_dir")

        # Check if parent directory exists and is writable, or can be created
        local parent_dir
        parent_dir=$(dirname "$input_dir")
        if [[ ! -d "$parent_dir" ]]; then
            if mkdir -p "$parent_dir" 2>/dev/null; then
                log "Created parent directory: $parent_dir"
            else
                echo "Error: Cannot create parent directory $parent_dir. Please check permissions or choose a different location."
                continue
            fi
        elif [[ ! -w "$parent_dir" ]]; then
             echo "Error: Parent directory $parent_dir is not writable. Please check permissions or choose a different location."
             continue
        fi

        echo "Installing to: $input_dir"
        read -p "Is this correct? (y/N): " confirm
        if [[ "$confirm" =~ ^[Yy]$ ]]; then
            INSTALL_DIR="$input_dir"
            LOG_FILE="${INSTALL_DIR}/install.log"
            mkdir -p "$(dirname "$LOG_FILE")"
            break
        fi
        # Loop back to ask again
    done
}

check_dependencies() {
    log "Checking for system package manager..."
    if command -v apt-get &> /dev/null; then
        PKG_MANAGER="apt"
        INSTALL_CMD="sudo apt update && sudo apt install -y"
        PYTHON_GI_PKG="python3-gi"
        GTK_PKG="gir1.2-gtk-3.0"
    elif command -v dnf &> /dev/null; then
        PKG_MANAGER="dnf"
        INSTALL_CMD="sudo dnf install -y"
        PYTHON_GI_PKG="python3-gobject"
        GTK_PKG="gtk3"
    elif command -v pacman &> /dev/null; then
        PKG_MANAGER="pacman"
        INSTALL_CMD="sudo pacman -Syu --noconfirm" # Note: Assumes system is up-to-date or user handles it
        PYTHON_GI_PKG="python-gobject"
        GTK_PKG="gtk3"
    else
        log "Warning: Could not identify package manager (apt, dnf, pacman). You might need to install dependencies manually."
        log "Required packages: Python 3, PyGObject bindings, GTK 3 development files."
        read -p "Press Enter to continue anyway, or Ctrl+C to abort..."
        return 0 # Don't fail, just warn
    fi
    log "Detected package manager: $PKG_MANAGER"
}

install_dependencies() {
    if [[ -n "$PKG_MANAGER" ]]; then
        log "Installing required system dependencies ($PYTHON_GI_PKG, $GTK_PKG)..."
        # Use eval to handle the command with potential sudo
        eval "$INSTALL_CMD $PYTHON_GI_PKG $GTK_PKG" || {
            log "Error: Failed to install dependencies using '$INSTALL_CMD'. Please install $PYTHON_GI_PKG and $GTK_PKG manually."
            exit 1
        }
        log "Dependencies installed successfully."
    fi
}

create_directories() {
    log "Creating installation directories..."
    # BIN_DIR is fixed to ~/.local/bin for user executables
    BIN_DIR="${HOME}/.local/bin"
    DESKTOP_DIR="${HOME}/.local/share/applications"
    ICON_DIR="${HOME}/.local/share/icons/hicolor/256x256/apps" # Standard size directory

    mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$DESKTOP_DIR" "$ICON_DIR" "$(dirname "$LOG_FILE")"
    log "Directories created."
}

copy_files() {
    log "Copying application files..."
    # Copy the main script
    if [[ -f "$APP_SCRIPT" ]]; then
        cp "$APP_SCRIPT" "$INSTALL_DIR/"
        log "Copied $APP_SCRIPT to $INSTALL_DIR/"
    else
        log "Error: Application script '$APP_SCRIPT' not found in the current directory."
        exit 1
    fi

    # Copy the icon if it exists locally
    if [[ -f "$APP_ICON_FILE" ]]; then
        cp "$APP_ICON_FILE" "$ICON_DIR/"
        log "Copied $APP_ICON_FILE to $ICON_DIR/"
    else
        log "Warning: Icon file '$APP_ICON_FILE' not found. Desktop entry might use a fallback icon."
        # You could potentially download a default icon here if desired
    fi
    log "File copying completed."
}

create_wrapper_script() {
    log "Creating executable wrapper script in ~/.local/bin..."
    # Wrapper script always goes to ~/.local/bin
    WRAPPER_PATH="${HOME}/.local/bin/$APP_EXEC_NAME"
    cat << EOF > "$WRAPPER_PATH"
#!/bin/bash
# Wrapper script for $APP_NAME
# Generated by install_vesselkeeper.sh
# Installation path: $INSTALL_DIR
exec python3 "$INSTALL_DIR/$APP_SCRIPT" "\$@"
EOF
    chmod +x "$WRAPPER_PATH"
    log "Wrapper script created at $WRAPPER_PATH"
}

create_desktop_entry() {
    log "Creating desktop entry..."
    DESKTOP_FILE_PATH="${HOME}/.local/share/applications/$APP_DESKTOP_FILE"
    # Determine the icon path for the desktop file
    if [[ -f "$APP_ICON_FILE" ]]; then
        ICON_PATH="${HOME}/.local/share/icons/hicolor/256x256/apps/$APP_ICON_FILE"
    else
        # Fallback icon name if no custom icon was provided/copied
        ICON_PATH="boat" # Or another standard icon name like applications-engineering
    fi

    cat << EOF > "$DESKTOP_FILE_PATH"
[Desktop Entry]
Version=1.0
Type=Application
Name=$APP_NAME
Comment=Vessel Management Application
Exec=$APP_EXEC_NAME
Icon=$ICON_PATH
Terminal=false
Categories=Office;
EOF
    chmod +x "$DESKTOP_FILE_PATH" # Make desktop file executable (good practice)
    log "Desktop entry created at $DESKTOP_FILE_PATH"
}

ensure_path() {
    log "Ensuring ~/.local/bin is in PATH..."
    BASHRC_FILE="${HOME}/.bashrc"
    ZSHRC_FILE="${HOME}/.zshrc"

    # Function to add PATH to a specific file
    add_path_to_file() {
        local file="$1"
        if [[ -f "$file" ]]; then
            if grep -qF 'export PATH="$HOME/.local/bin:$PATH"' "$file"; then
                log "~/.local/bin already in PATH in $file"
            else
                log "Adding ~/.local/bin to PATH in $file"
                echo "" >> "$file" # Add a newline for neatness
                echo "# Added by VesselKeeper installer" >> "$file"
                echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$file"
                log "Please restart your terminal or run 'source $(basename "$file")' for PATH changes to take effect."
            fi
        fi
    }

    # Check for common shell config files and add PATH if needed
    if [[ -f "$BASHRC_FILE" ]]; then
        add_path_to_file "$BASHRC_FILE"
    fi
    if [[ -f "$ZSHRC_FILE" ]]; then
        add_path_to_file "$ZSHRC_FILE"
    fi

    # If neither file exists, create .bashrc and add it
    if [[ ! -f "$BASHRC_FILE" && ! -f "$ZSHRC_FILE" ]]; then
        log "No standard shell config file found. Creating ~/.bashrc and adding PATH."
        touch "$BASHRC_FILE"
        add_path_to_file "$BASHRC_FILE"
    fi
}


finalize_installation() {
    log "Finalizing installation..."
    # Update desktop database (might require gtk-update-icon-cache, but less critical for user dirs)
    # Inform user
    log "Installation completed successfully!"
    echo
    log "=============================================================="
    log " $APP_NAME has been installed to $INSTALL_DIR!"
    log "--------------------------------------------------------------"
    log " To run from the terminal, you might need to either:"
    log "   1. Restart your terminal, or"
    log "   2. Run 'source ~/.bashrc' (or ~/.zshrc if using zsh)"
    log " You can then launch it by running '$APP_EXEC_NAME'"
    log " You can also launch it from your Applications/Office menu."
    log "=============================================================="
    echo
}

# --- Main Execution ---

main() {
    echo "Starting $APP_NAME installation..."
    prompt_for_install_dir
    log "Starting $APP_NAME installation to $INSTALL_DIR..."

    check_dependencies
    install_dependencies
    create_directories
    copy_files
    create_wrapper_script
    create_desktop_entry
    ensure_path # Add this step to handle PATH
    finalize_installation

    log "Installation script finished."
}

# Run the main function
main "$@"

