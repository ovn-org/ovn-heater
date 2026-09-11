#!/bin/bash
# CI helper script for running ovn-heater tests inside a QEMU/KVM VM.
#
# Usage:
#   ci.sh prepare   - Download a cloud image, install dependencies,
#                      and compress it for caching.
#   ci.sh run       - Boot the cached image and run the test suite.
#
# Requires OS_TYPE (fedora or ubuntu) to be set.

set -o errexit
set -x

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOPDIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

OS_TYPE="${OS_TYPE:?OS_TYPE required (fedora or ubuntu)}"

# ====================================================================
# VM helpers
# ====================================================================

VM_SSH_PORT=2222
VM_PIDFILE=/tmp/vm.pid
VM_NET=10.0.2.0/24
VM_GUEST_IP=10.0.2.42
VM_IMAGE_SIZE="20G"

_VM_SSH_OPTS=(
    -p "$VM_SSH_PORT"
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o ConnectTimeout=5
    -o BatchMode=yes
    -o ServerAliveInterval=15
    -o ServerAliveCountMax=4
    -o LogLevel=ERROR
)

vm_ssh() {
    ssh "${_VM_SSH_OPTS[@]}" -i "${VM_SSH_KEY}" \
        root@localhost "$@"
}

vm_rsync_to() {
    local src="${1:?source required}"
    local dst="${2:?destination required}"

    rsync -az --delete \
        -e "ssh ${_VM_SSH_OPTS[*]} -i ${VM_SSH_KEY}" \
        "${src}" "root@localhost:${dst}"
}

vm_rsync_from() {
    local src="${1:?source required}"
    local dst="${2:?destination required}"

    rsync -az \
        -e "ssh ${_VM_SSH_OPTS[*]} -i ${VM_SSH_KEY}" \
        "root@localhost:${src}" "${dst}"
}

# vm_start <image> <seed_iso>
vm_start() {
    local img="${1:?image file required}"
    local seed_iso="${2:?seed ISO required}"

    qemu-system-x86_64 \
        -enable-kvm -cpu host \
        -m 14336 -smp 4 \
        -nographic \
        -netdev "user,id=net0,net=${VM_NET},dhcpstart=${VM_GUEST_IP},hostfwd=tcp::${VM_SSH_PORT}-:22" \
        -device virtio-net-pci,netdev=net0 \
        -drive "file=${img},if=virtio,format=qcow2,cache=unsafe" \
        -device virtio-rng-pci \
        -pidfile "${VM_PIDFILE}" \
        -drive "if=virtio,id=seed,file=${seed_iso},format=raw,media=cdrom,readonly=on" \
        > /tmp/vm-console.log 2>&1 &

    echo "VM launched (PID $!); log: /tmp/vm-console.log"
}

vm_stop() {
    local pid

    [ -f "${VM_PIDFILE}" ] || return 0
    pid=$(cat "${VM_PIDFILE}" 2>/dev/null) || return 0

    vm_ssh "shutdown -h now" 2>/dev/null || true

    local i
    for i in $(seq 1 30); do
        kill -0 "${pid}" 2>/dev/null || {
            rm -f "${VM_PIDFILE}"
            return 0
        }
        sleep 2
    done

    kill "${pid}" 2>/dev/null || true
    rm -f "${VM_PIDFILE}"
}

# vm_wait_ssh <max_attempts> <delay>
vm_wait_ssh() {
    local max="${1}" delay="${2}" i

    echo "Waiting for SSH on port ${VM_SSH_PORT} ..."
    for i in $(seq 1 "${max}"); do
        if vm_ssh true 2>/dev/null; then
            echo "SSH ready (attempt ${i})."
            return 0
        fi
        echo "  attempt ${i}/${max} ..."
        [ "${i}" != "${max}" ] && sleep "${delay}"
    done

    echo "ERROR: SSH not available after $((max * delay))s." >&2
    return 1
}

# vm_wait_cloud_init <max_attempts> <delay>
# Waits until cloud-init has finished its run.  Checks the output
# text rather than the exit code because cloud-init may return
# non-zero even when the status is "done" (e.g., due to recoverable
# errors during package installation).
vm_wait_cloud_init() {
    local max="${1}" delay="${2}" i

    echo "Waiting for cloud-init to complete ..."
    for i in $(seq 1 "${max}"); do
        local output
        output=$(vm_ssh "cloud-init status --wait" 2>/dev/null) || true
        echo "  cloud-init output: ${output}"
        if echo "${output}" | grep -q 'status: done'; then
            echo "cloud-init complete (attempt ${i})."
            return 0
        fi
        echo "  attempt ${i}/${max} ..."
        sleep "${delay}"
    done

    echo "ERROR: cloud-init not finished after $((max * delay))s." >&2
    return 1
}

# vm_create_seed <pubkey_file> <work_dir> <output_iso> [install_packages]
# Creates a NoCloud seed ISO with SSH key injection.
# When install_packages is "true" (default), the seed also includes
# package installation commands (used during image preparation).
# Pass "false" for test runs where the cached image already has
# all packages installed.
vm_create_seed() {
    local pub_key_file="${1:?public key file required}"
    local work_dir="${2:?work dir required}"
    local out_iso="${3:?output ISO required}"
    local install_packages="${4:-true}"
    local pub_key

    pub_key=$(cat "${pub_key_file}")
    mkdir -p "${work_dir}"

    cat > "${work_dir}/meta-data" <<EOF
instance-id: ovn-heater-ci
local-hostname: ovn-heater-ci
EOF

    cat > "${work_dir}/user-data" <<EOF
#cloud-config
users:
  - name: root
    ssh_authorized_keys:
      - ${pub_key}

runcmd:
  - sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
  - systemctl restart sshd || service sshd restart || true
EOF

    if [ "${install_packages}" = "true" ]; then
        cat >> "${work_dir}/user-data" <<'PKGEOF'

package_update: true
PKGEOF

        if [ "${OS_TYPE}" = "fedora" ]; then
            cat >> "${work_dir}/user-data" <<'FEDEOF'
packages:
  - git
  - ansible
  - podman
  - ansible-collection-ansible-posix
  - ansible-collection-ansible-utils
FEDEOF
        elif [ "${OS_TYPE}" = "ubuntu" ]; then
            cat >> "${work_dir}/user-data" <<'UBEOF'
packages:
  - git
  - ansible
  - podman
UBEOF
        fi
    fi

    genisoimage -output "${out_iso}" \
        -volid cidata -rational-rock -joliet \
        "${work_dir}/user-data" "${work_dir}/meta-data" 2>/dev/null

    echo "Seed ISO created: ${out_iso}"
}

# ====================================================================
# Commands
# ====================================================================

KEY_DIR="$(mktemp -d)"
SSH_KEY="${KEY_DIR}/id_ed25519"
ssh-keygen -t ed25519 -f "${SSH_KEY}" -N "" -q
export VM_SSH_KEY="${SSH_KEY}"

# -- prepare ---------------------------------------------------------
# Downloads the cloud image, boots it with cloud-init to install
# packages, then compresses the result for caching.
cmd_prepare() {
    case "${OS_TYPE}" in
        fedora)
            IMAGE_URL="https://download.fedoraproject.org/pub/fedora/linux/releases/43/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2"
            ;;
        ubuntu)
            IMAGE_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
            ;;
        *)
            echo "ERROR: Unknown OS_TYPE: ${OS_TYPE}" >&2
            exit 1
            ;;
    esac

    local out_img="${OS_TYPE}-ci.qcow2"

    echo "Downloading ${OS_TYPE} cloud image ..."
    curl -fL "${IMAGE_URL}" -o "${out_img}.dl"

    # Convert to qcow2 in case the download is a raw or other format.
    qemu-img convert -O qcow2 "${out_img}.dl" "${out_img}"
    rm -f "${out_img}.dl"

    # Resize disk to 40GB for CI workloads (container images, builds).
    qemu-img resize "${out_img}" "${VM_IMAGE_SIZE}"

    vm_create_seed "${SSH_KEY}.pub" /tmp/vm-seed /tmp/vm-seed.iso true

    vm_start "${out_img}" /tmp/vm-seed.iso

    local vm_stopped=false
    cleanup_prepare() {
        if ! ${vm_stopped}; then
            vm_rsync_from /var/log/cloud-init-output.log ./ \
                2>/dev/null || true
            vm_stop
        fi
        cp /tmp/vm-console.log ./vm-console.log 2>/dev/null || true
        rm -rf "${KEY_DIR}" /tmp/vm-seed /tmp/vm-seed.iso
    }
    trap cleanup_prepare EXIT

    # Wait for cloud-init to finish installing packages.
    vm_wait_ssh 30 10
    vm_wait_cloud_init 30 10

    # Grow the root filesystem to fill the resized disk.
    vm_ssh "growpart /dev/vda 1 || true"
    vm_ssh "resize2fs /dev/vda1 || xfs_growfs / || true"

    # Verify key dependencies are installed.
    vm_ssh "which git ansible podman"

    # Reset cloud-init state so it re-runs on the next boot to
    # inject per-job SSH keys.
    vm_ssh "cloud-init clean"

    vm_stop
    vm_stopped=true

    # Compress the image for caching.
    qemu-img convert -c -O qcow2 "${out_img}" "${out_img}.tmp"
    mv "${out_img}.tmp" "${out_img}"

    echo "Image ready: ${out_img} ($(du -sh "${out_img}" | cut -f1))"
}

# -- run -------------------------------------------------------------
# Boots the cached VM image and runs the ovn-heater test suite.
cmd_run() {
    local base_img="${OS_TYPE}-ci.qcow2"
    local run_img="${OS_TYPE}-run.qcow2"

    if [ ! -f "${base_img}" ]; then
        echo "ERROR: ${base_img} not found." >&2
        exit 1
    fi

    # COW overlay keeps the cached base image unmodified.
    qemu-img create -f qcow2 -F qcow2 \
        -b "$(realpath "${base_img}")" "${run_img}"

    vm_create_seed "${SSH_KEY}.pub" /tmp/vm-seed /tmp/vm-seed.iso false

    vm_start "${run_img}" /tmp/vm-seed.iso

    cleanup_run() {
        mkdir -p "${TOPDIR}/test_results"
        vm_rsync_from /root/ovn-heater/test_results/ \
            "${TOPDIR}/test_results/" 2>/dev/null || true
        vm_rsync_from /var/log/cloud-init-output.log ./ \
            2>/dev/null || true
        vm_stop
        cp /tmp/vm-console.log ./vm-console.log 2>/dev/null || true
        rm -rf "${KEY_DIR}" "${run_img}" /tmp/vm-seed /tmp/vm-seed.iso
    }
    trap cleanup_run EXIT

    vm_wait_ssh 30 10
    vm_wait_cloud_init 12 10

    # Grow the root filesystem (COW overlay inherits the resized disk).
    vm_ssh "growpart /dev/vda 1 || true"
    vm_ssh "resize2fs /dev/vda1 || xfs_growfs / || true"

    # Use the VM's known IP for the CI host address.
    local ci_host="${VM_GUEST_IP}"

    # Configure SSH inside VM for ansible (self-host).
    vm_ssh "mkdir -p /root/.ssh/ && \
            ssh-keygen -t rsa -N '' -q -f /root/.ssh/id_rsa && \
            ssh-keyscan \$(hostname) ${ci_host} \
                >> /root/.ssh/known_hosts && \
            cat /root/.ssh/id_rsa.pub >> /root/.ssh/authorized_keys && \
            chmod og-wx /root/.ssh/authorized_keys && \
            ssh root@\$(hostname) echo Hello && \
            ssh root@${ci_host} echo Hello"

    # Copy ovn-heater source into the VM.
    vm_ssh "mkdir -p /root/ovn-heater"
    vm_rsync_to "${TOPDIR}/" /root/ovn-heater/

    # Configure the CI physical deployment with the VM IP.
    vm_ssh "sed -i \"s/<host>/${ci_host}/g\" \
                /root/ovn-heater/physical-deployments/ci.yml"

    # Restore runtime cache if available (container images, built sources).
    if [ -f "${TOPDIR}/runtime-cache/runtime.tar.gz" ]; then
        echo "Restoring runtime cache ..."
        vm_rsync_to "${TOPDIR}/runtime-cache/runtime.tar.gz" /root/
        vm_ssh "cd /root/ovn-heater && \
                mkdir -p runtime && \
                tar -xzf /root/runtime.tar.gz && \
                podman load -i \
                    runtime/ovn-fake-multinode/ovn-multi-node-image.tar \
                    || true"
    fi

    # Set PHYS_DEPLOYMENT and run install.
    # On Ubuntu, use ubuntu-based ovn-fake-multinode images.
    local phys=/root/ovn-heater/physical-deployments/ci.yml
    if [ "${OS_TYPE}" = "ubuntu" ]; then
        vm_ssh "cd /root/ovn-heater && \
                export PHYS_DEPLOYMENT=${phys} && \
                export OS_BASE=ubuntu && \
                export OS_IMAGE_OVERRIDE=ubuntu:rolling && \
                ./do.sh install"
    else
        vm_ssh "cd /root/ovn-heater && \
                export PHYS_DEPLOYMENT=${phys} && \
                ./do.sh install"
    fi

    # Save runtime cache for future runs.
    echo "Saving runtime cache ..."
    vm_ssh "cd /root/ovn-heater && \
            tar -czf /root/runtime.tar.gz runtime"
    mkdir -p "${TOPDIR}/runtime-cache"
    vm_rsync_from /root/runtime.tar.gz "${TOPDIR}/runtime-cache/"

    # Enable verbose logging for test scenarios.
    vm_ssh "cd /root/ovn-heater && \
            sed -i 's/^  log_cmds\: False/  log_cmds\: True/' \
                test-scenarios/ovn-low-scale*.yml && \
            sed -i 's/^  log_cmds\: false/  log_cmds\: true/' \
                test-scenarios/openstack-low-scale.yml && \
            sed -i 's/^  log_cmds\: false/  log_cmds\: true/' \
                test-scenarios/openstack-bgp.yml"

    # Run tests.
    vm_ssh "cd /root/ovn-heater && \
            export PHYS_DEPLOYMENT=${phys} && \
            ./do.sh run test-scenarios/ovn-low-scale.yml low-scale"

    vm_ssh "cd /root/ovn-heater && \
            export PHYS_DEPLOYMENT=${phys} && \
            ./do.sh run \
                test-scenarios/ovn-low-scale-ic.yml low-scale-ic"

    vm_ssh "cd /root/ovn-heater && \
            export PHYS_DEPLOYMENT=${phys} && \
            ./do.sh run \
                test-scenarios/openstack-low-scale.yml \
                openstack-low-scale"

    vm_ssh "cd /root/ovn-heater && \
            export PHYS_DEPLOYMENT=${phys} && \
            ./do.sh run \
                test-scenarios/openstack-bgp.yml \
                openstack-bgp"

    # Check logs for failures.
    vm_ssh "cd /root/ovn-heater && ./utils/logs-checker.sh"
}

# -- main ------------------------------------------------------------
case "${1:-}" in
    prepare) cmd_prepare ;;
    run)     cmd_run ;;
    *)
        echo "Usage: $0 <prepare|run>" >&2
        exit 1
        ;;
esac
