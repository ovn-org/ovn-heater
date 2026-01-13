# We want values from both the `ID` and `ID_LIKE` fields to ensure successful
# categorization.  The shell will happily accept both spaces and newlines as
# separators:
# https://pubs.opengroup.org/onlinepubs/9699919799/utilities/V3_chap02.html#tag_18_06_05
DISTRO_IDS=$(awk -F= '/^ID/{print$2}' /etc/os-release | tr -d '"')

DISTRO_VERSION_ID=$(awk -F= '/^VERSION_ID/{print$2}' /etc/os-release | tr -d '"')

function is_rpm_based() {
    for id in $DISTRO_IDS; do
        case $id in
        centos* | rhel* | fedora*)
            true
            return
        ;;
        esac
    done
    false
}

function is_rhel() {
    for id in $DISTRO_IDS; do
        case $id in
        centos* | rhel*)
            true
            return
        ;;
        esac
    done
    false
}

function is_fedora() {
    for id in $DISTRO_IDS; do
        case $id in
        fedora*)
            true
            return
        ;;
        esac
    done
    false
}

function is_deb_based() {
    for id in $DISTRO_IDS; do
        case $id in
        debian* | ubuntu*)
            true
            return
            ;;
        esac
    done
    false
}

# Installs the most recent available python3 and pip version for the
# current distro.
function install_latest_python() {
    if is_rhel
    then
        py_pkg=$(dnf list available 'python3.[0-9][0-9]' -q 2> /dev/null |
                 grep -o '^python3.[0-9][0-9]' | sort -V | tail -n 1) &&
        dnf install -y ${py_pkg}-pip &&
        alternatives --install /usr/bin/python3 python3 /usr/bin/$py_pkg 100
    elif is_fedora
    then
        dnf install -y python3-pip
    elif is_deb_based
    then
        apt install -y python3-pip
    else
        die_distro
    fi
}

function die() {
    echo $1
    exit 1
}

function die_distro() {
    die "Unable to determine distro type, rpm- and deb-based are supported."
}
