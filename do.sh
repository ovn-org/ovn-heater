#!/bin/bash

set -o errexit -o pipefail

topdir=$(pwd)
rundir_name=runtime
rundir=${topdir}/${rundir_name}
deployment_dir=${topdir}/physical-deployments
result_dir=${topdir}/test_results

phys_deployment="${PHYS_DEPLOYMENT:-${deployment_dir}/physical-deployment.yml}"
ovn_heater_venv=venv

clustered_db=${CLUSTERED_DB:-True}

ovn_fmn_utils=${topdir}/ovn-fake-multinode-utils
ovn_fmn_playbooks=${ovn_fmn_utils}/playbooks
ovn_fmn_generate=${ovn_fmn_utils}/generate-hosts.py
ovn_fmn_docker=${ovn_fmn_utils}/generate-docker-cfg.py
ovn_fmn_podman=${ovn_fmn_utils}/generate-podman-cfg.py
ovn_fmn_get=${ovn_fmn_utils}/get-config-value.py
ovn_fmn_ip=${rundir}/ovn-fake-multinode/ip_gen.py
ovn_fmn_translate=${ovn_fmn_utils}/translate_yaml.py
hosts_file=${rundir}/hosts
installer_log_file=${rundir}/installer-log
docker_daemon_file=${rundir}/docker-daemon.json
podman_registry_file=${rundir}/registries.conf
log_collector_file=${rundir}/log-collector.sh
log_perf_file=${rundir}/perf.sh
process_monitor_file=${rundir}/process-monitor.py

ovn_tester=${topdir}/ovn-tester

EXTRA_OPTIMIZE=${EXTRA_OPTIMIZE:-no}
USE_OVSDB_ETCD=${USE_OVSDB_ETCD:-no}

# We want values from both the `ID` and `ID_LIKE` fields to ensure successful
# categorization.  The shell will happily accept both spaces and newlines as
# separators:
# https://pubs.opengroup.org/onlinepubs/9699919799/utilities/V3_chap02.html#tag_18_06_05
DISTRO_IDS=$(awk -F= '/^ID/{print$2}' /etc/os-release | tr -d '"')

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

function die() {
    echo $1
    exit 1
}

function die_distro() {
    die "Unable to determine distro type, rpm- and deb-based are supported."
}

function generate() {
    # Make sure rundir exists.
    mkdir -p ${rundir}

    PYTHONPATH=${topdir}/utils ${ovn_fmn_generate} ${phys_deployment} ${rundir} ${ovn_fmn_repo} ${ovn_fmn_branch} > ${hosts_file}
    PYTHONPATH=${topdir}/utils ${ovn_fmn_docker} ${phys_deployment} > ${docker_daemon_file}
    PYTHONPATH=${topdir}/utils ${ovn_fmn_podman} ${phys_deployment} > ${podman_registry_file}
    cp ${ovn_fmn_utils}/process-monitor.py ${process_monitor_file}
    cp ${ovn_fmn_utils}/scripts/log-collector.sh ${log_collector_file}
    cp ${ovn_fmn_utils}/scripts/perf.sh ${log_perf_file}
}

function install_deps_local_rpm() {
    echo "-- Installing local dependencies"
    yum install redhat-lsb-core datamash \
        python3-pip python3-virtualenv python3-netaddr python3 python3-devel \
        python-virtualenv podman podman-docker \
        --skip-broken -y
    [ -e /usr/bin/pip ] || ln -sf /usr/bin/pip3 /usr/bin/pip

}

function install_deps_local_deb() {
    echo "-- Installing local dependencies"
    apt -y install datamash podman podman-docker python3-pip \
           python3-virtualenv python3-netaddr python3 python3-all-dev
}

function install_deps_remote() {
    echo "-- Installing dependencies on all nodes"
    ansible-playbook ${ovn_fmn_playbooks}/install-dependencies.yml \
        -i ${hosts_file}
}

function run_registry() {
    containers=$(docker ps --all --filter='name=(ovn|registry)' \
                        | grep -v "CONTAINER ID" | awk '{print $1}' || true)
    for container_name in $containers
    do
        docker stop $container_name
        docker rm $container_name
    done
    [ -d /var/lib/registry ] || mkdir /var/lib/registry -p
    docker run --privileged -d --name registry -p 5000:5000 \
          -v /var/lib/registry:/var/lib/registry --restart=always docker.io/library/registry:2

    # This is requried on the orchestrator for local image build/push to work
    cp /etc/containers/registries.conf /etc/containers/registries.conf.bak
    cat > /etc/containers/registries.conf << EOF
[registries.insecure]
registries = ['localhost:5000']
[registries.block]
registries = []
EOF
}

function install_venv() {
    pushd ${rundir}
    if [ ! -f ${ovn_heater_venv}/bin/activate ]; then
        rm -rf ${ovn_heater_venv}
        python3 -m virtualenv ${ovn_heater_venv}
    fi
    source ${ovn_heater_venv}/bin/activate
    if is_rpm_based; then
        python3 -m ensurepip
    fi
    python3 -m pip install -r ${topdir}/utils/requirements.txt
    deactivate
    popd
}

function configure_docker() {
    echo "-- Configuring local registry on tester nodes"
    if which podman
    then
        echo "-- Configuring podman local registry on all nodes"
        ansible-playbook ${ovn_fmn_playbooks}/configure-podman-registry.yml -i ${hosts_file}
    else
        echo "-- Configuring docker local registry on all nodes"
        ansible-playbook ${ovn_fmn_playbooks}/configure-docker-registry.yml -i ${hosts_file}
    fi

}

function clone_component() {
    local comp_name=$1
    local comp_repo=$2
    local comp_branch=$3

    pushd ${rundir}
    local comp_exists="0"
    if [ -d ${comp_name} ]; then
        pushd ${comp_name}
        local remote=$(git config --get remote.origin.url)
        if [ "${remote}" = "${comp_repo}" ]; then
            git fetch origin

            if $(git show-ref --verify refs/tags/${comp_branch} &> /dev/null); then
                local branch_diff=$(git diff ${comp_branch} HEAD --stat | wc -l)
            else
                local branch_diff=$(git diff origin/${comp_branch} HEAD --stat | wc -l)
            fi
            if [ "${branch_diff}" = "0" ]; then
                comp_exists="1"
            fi
        fi
        popd
    fi

    if [ ${comp_exists} = "1" ]; then
        echo "-- Component ${comp_name} already installed"
        return 0
    else
        rm -rf ${comp_name}
        echo "-- Cloning ${comp_name} from ${comp_repo} at revision ${comp_branch}"
        git clone ${comp_repo} ${comp_name} --branch ${comp_branch} --single-branch --depth 1
        return 1
    fi
    popd
}

# OVS/OVN env vars
ovs_repo="${OVS_REPO:-https://github.com/openvswitch/ovs.git}"
ovs_branch="${OVS_BRANCH:-master}"
ovn_repo="${OVN_REPO:-https://github.com/ovn-org/ovn.git}"
ovn_branch="${OVN_BRANCH:-main}"

# ovn-fake-multinode env vars
ovn_fmn_repo="${OVN_FAKE_MULTINODE_REPO:-https://github.com/ovn-org/ovn-fake-multinode.git}"
ovn_fmn_branch="${OVN_FAKE_MULTINODE_BRANCH:-main}"

OS_IMAGE_OVERRIDE="${OS_IMAGE_OVERRIDE}"

function install_ovn_fake_multinode() {
    echo "-- Cloning ${ovn_fmn_repo} on all nodes, revision ${ovn_fmn_branch}"

    local rebuild_needed=0

    # Clone repo locally
    clone_component ovn-fake-multinode ${ovn_fmn_repo} ${ovn_fmn_branch} || rebuild_needed=1

    # Copy repo to all hosts.
    ansible-playbook ${ovn_fmn_playbooks}/install-fake-multinode.yml -i ${hosts_file} \
        --extra-vars="ovn_fake_multinode_local_path=${rundir}/ovn-fake-multinode"

    if [ -n "$RPM_OVS" ]
    then
        [ -d ovs ] || { rm -rf ovs; mkdir ovs; }
        rebuild_needed=1
    else
        clone_component ovs ${ovs_repo} ${ovs_branch} || rebuild_needed=1
    fi

    if [ -n "$RPM_OVN_COMMON" ]
    then
        [ -d ovn ] || { rm -rf ovn; mkdir ovn; }
        rebuild_needed=1
    else
        clone_component ovn ${ovn_repo} ${ovn_branch} || rebuild_needed=1
    fi


    pushd ${rundir}/ovn-fake-multinode

    [ -n "$RPM_OVS" ] && wget $RPM_OVS
    [ -n "$RPM_SELINUX" ] && wget $RPM_SELINUX
    if [ -n "$RPM_OVN_COMMON" ]
    then
        wget $RPM_OVN_COMMON
        rpm_v=`basename $RPM_OVN_COMMON | awk -F '-' '{print $1}'`
        rpm_b=`basename $RPM_OVN_COMMON | sed 's/^'"$rpm_v"'\(.*\)/\1/'`
        RPM_OVN_CENTRAL=${RPM_OVN_CENTRAL:-"$(dirname $RPM_OVN_COMMON)/$rpm_v-central$rpm_b"}
        RPM_OVN_HOST=${RPM_OVN_HOST:-"$(dirname $RPM_OVN_COMMON)/$rpm_v-host$rpm_b"}
        [ -n "$RPM_OVN_CENTRAL" ] && wget $RPM_OVN_CENTRAL
        [ -n "$RPM_OVN_HOST" ] && wget $RPM_OVN_HOST
    fi

    docker images | grep -q 'ovn/ovn-multi-node' || rebuild_needed=1

    if [ ${rebuild_needed} -eq 1 ]; then
        if [ -z "${OS_IMAGE_OVERRIDE}" ]; then
            os_release=$(lsb_release -r | awk '{print $2}')
            os_release=${os_release:-"32"}
            if grep Fedora /etc/redhat-release
            then
                os_image="fedora:$os_release"
            elif grep "Red Hat Enterprise Linux" /etc/redhat-release
            then
                [[ "$os_release" =~ 7\..* ]] && os_image="registry.access.redhat.com/ubi7/ubi:$os_release"
                [[ "$os_release" =~ 8\..* ]] && os_image="registry.access.redhat.com/ubi8/ubi:$os_release"
            fi
        else
            os_image=${OS_IMAGE_OVERRIDE}
        fi

        # Build images locally.
        OS_IMAGE=$os_image OVS_SRC_PATH=${rundir}/ovs OVN_SRC_PATH=${rundir}/ovn EXTRA_OPTIMIZE=${EXTRA_OPTIMIZE} USE_OVSDB_ETCD=${USE_OVSDB_ETCD} ./ovn_cluster.sh build
    fi
    # Tag and push image
    docker tag ovn/ovn-multi-node localhost:5000/ovn/ovn-multi-node
    docker push localhost:5000/ovn/ovn-multi-node
    popd
}

function install_ovn_tester() {
    ssh_key=$(${ovn_fmn_get} ${phys_deployment} tester-node ssh_key)
    # We need to copy the files into a known directory within the Docker
    # context directory. Otherwise, Docker can't find the files we reference.
    cp ${ssh_key} .
    ssh_key_file=${rundir_name}/$(basename ${ssh_key})
    docker build -t ovn/ovn-tester --build-arg SSH_KEY=${ssh_key_file} -f ${topdir}/Dockerfile ${topdir}
    docker tag ovn/ovn-tester localhost:5000/ovn/ovn-tester
    docker push localhost:5000/ovn/ovn-tester
}

# Prepare OVS bridges and cleanup containers.
function init_ovn_fake_multinode() {
    echo "-- Initializing ovn-fake-multinode cluster on all nodes"
    ansible-playbook ${ovn_fmn_playbooks}/deploy-minimal.yml -i ${hosts_file}
}

# Pull image on all nodes
function pull_ovn_fake_multinode() {
    # Pull image on all nodes
    ansible-playbook ${ovn_fmn_playbooks}/pull-fake-multinode.yml -i ${hosts_file}
}

function pull_ovn_tester() {
    ansible-playbook ${ovn_fmn_playbooks}/pull-ovn-tester.yml -i ${hosts_file}
}

function install() {
    pushd ${rundir}
    if is_rpm_based
    then
        install_deps_local_rpm
    elif is_deb_based
    then
        install_deps_local_deb
    else
        die_distro
    fi
    install_deps_remote
    run_registry
    install_venv
    configure_docker
    install_ovn_fake_multinode
    init_ovn_fake_multinode
    pull_ovn_fake_multinode
    install_ovn_tester
    pull_ovn_tester
    popd
}

function translate_yaml() {
    local test_file=$1

    pushd ${rundir} > /dev/null
    source ${ovn_heater_venv}/bin/activate
    translated_test_file=${rundir}/test-scenario.yml
    ${ovn_fmn_translate} ${test_file} ${translated_test_file}
    deactivate
    popd > /dev/null

    echo ${translated_test_file}
}

function record_test_config() {
    local out_dir=$1
    local out_file=${out_dir}/config
    local out_installer_log_file=${out_dir}/installer-log
    local out_hosts_file=${out_dir}/hosts

    echo "-- Storing installer log in ${install_log_file}"
    cp ${installer_log_file} ${out_installer_log_file}

    echo "-- Storing hosts file in ${out_hosts_file}"
    cp ${hosts_file} ${out_hosts_file}

    echo "-- Storing test components versions in ${out_file}"
    > ${out_file}

    components=("ovn-fake-multinode" "ovs" "ovn")
    for d in "${components[@]}"; do
        pushd ${rundir}/$d
        local origin=$(git config --get remote.origin.url)
        local sha=$(git rev-parse HEAD)
        local sha_name=$(git rev-parse --abbrev-ref HEAD)
        echo "$d (${origin}): ${sha} (${sha_name})" >> ${out_file}
        popd
    done
}

function mine_data() {
    out_dir=$1
    tester_host=$2

    echo "-- Mining data from logs in: ${out_dir}"

    pushd ${out_dir}

    mkdir -p mined-data
    for p in ovn-northd ovn-controller ovn-nbctl; do
        logs=$(find ${out_dir}/logs -name ${p}.log)
        ${topdir}/utils/mine-poll-intervals.sh ${logs} > mined-data/${p}
    done
    for p in ovsdb-server-sb ovsdb-server-nb; do
        logs=$(find ${out_dir}/logs -name ${p}.log)
        ${topdir}/utils/mine-db-poll-intervals.sh ${logs} > mined-data/${p}
    done

    cat ${out_dir}/test-log | grep 'Binding lport' \
        | cut -d ' ' -f 1,2,8 > mined-data/ovn-binding.log

    logs=$(find ${out_dir}/logs -name ovn-controller.log)
    grep ovn-installed ${logs} | cut -d ':' -f 2- | tr '|' ' ' \
        | cut -d ' ' -f 1,7 | tr 'T' ' ' | sort > mined-data/ovn-installed.log

    source ${rundir}/${ovn_heater_venv}/bin/activate
    python3 ${topdir}/utils/latency.py \
        ./mined-data/ovn-binding.log ./mined-data/ovn-installed.log \
        > mined-data/binding-to-ovn-installed-latency

    rm -rf ./mined-data/ovn-binding.log ./mined-data/ovn-installed.log

    logs=$(find ${out_dir}/logs -iname 'ps*')
    grep died ${logs} | sed 's/.*\/\(ovn-.*\)/\1/' > mined-data/crashes
    [ -s mined-data/crashes ] || rm -f mined-data/crashes

    resource_usage_logs=$(find ${out_dir}/logs -name process-stats.json \
                            | grep -v ovn-scale)
    python3 ${topdir}/utils/process-stats.py \
        resource-usage-report-central.html ${resource_usage_logs}

    # Collecting stats only for 3 workers to avoid bloating the report.
    resource_usage_logs=$(find ${out_dir}/logs -name process-stats.json \
                            | grep ovn-scale | head -3)
    python3 ${topdir}/utils/process-stats.py \
        resource-usage-report-worker.html ${resource_usage_logs}
    deactivate

    popd
}

function get_cluster_var() {
    local test_file=$1
    local var_name=$2

    var=$(${ovn_fmn_get} ${test_file} cluster ${var_name})

    if [ "${var_name}" == "clustered_db" ]; then
        if [ "${var}" == "True" ]; then
            echo -n "n_central=3 "
        else
            echo -n "n_central=1 "
        fi
    fi

    if [ "${var}" == "True" ]; then
        echo "${var_name}=yes";
    elif [ "${var}" == "False" ]; then
        echo "${var_name}=no"
    else
        echo "${var_name}=${var}"
    fi
}

function run_test() {
    local test_file=$1
    local out_dir=$2
    shift; shift

    # Make sure results dir exists.
    mkdir -p ${out_dir}/logs

    # Record SHAs of all components.
    record_test_config ${out_dir}

    # Perform a fast cleanup by doing a minimal redeploy.
    init_ovn_fake_multinode

    cluster_vars=""
    for var in enable_ssl clustered_db monitor_all use_ovsdb_etcd \
               node_net datapath_type n_relays n_workers; do
        cluster_vars="${cluster_vars} $(get_cluster_var ${test_file} ${var})"
    done
    echo "-- Cluster vars: ${cluster_vars}"

    if ! ansible-playbook ${ovn_fmn_playbooks}/bringup-cluster.yml \
           -i ${hosts_file} --extra-vars "${cluster_vars}" ; then
        die "-- Failed to bring up fake cluster!"
    fi

    if ! ansible-playbook ${ovn_fmn_playbooks}/configure-tester.yml -i ${hosts_file} \
           --extra-vars "test_file=${test_file} phys_deployment=${phys_deployment}" ; then
        die "-- Failed to set up test!"
    fi

    tester_host=$(${ovn_fmn_get} ${phys_deployment} tester-node name)
    if ! ssh root@${tester_host} docker exec \
            ovn-tester python3 -u /ovn-tester/ovn_tester.py \
                                  /physical-deployment.yml /test-scenario.yml ;
    then
        echo "-- Failed to run test. Check logs at: ${out_dir}/test-log"
    fi

    echo "-- Collecting logs to: ${out_dir}"
    ansible-playbook ${ovn_fmn_playbooks}/collect-logs.yml -i ${hosts_file} \
        --extra-vars "results_dir=${out_dir}/logs"

    pushd ${out_dir}/logs
    for f in *.tgz; do
       tar xvfz $f
    done
    # Prior to containerization of ovn-tester, HTML files written by ovn-tester
    # were written directly to ${out_dir}. To make things easier for tools, we
    # copy the HTML files back to this original location.
    cp ${tester_host}/ovn-tester/*.html ${out_dir} || true

    # Once we successfully ran the test and collected its logs, the post
    # processing (e.g., data mining) can run in a subshell with errexit
    # disabled.  We don't want the whole thing to error out if the post
    # processing fails.
    (
        set +o errexit
        mine_data ${out_dir} ${tester_host}
    )
}

function usage() {
    die "Usage: $0 install|generate|init|run <scenario> <out-dir>"
}

do_lockfile=/tmp/do.sh.lock

function take_lock() {
    exec 42>${do_lockfile} || die "Failed setting FD for ${do_lockfile}"
    flock -n 42 || die "Error: ovn-heater ($1) already running"
}

case "${1:-"usage"}" in
    "install")
        ;&
    "generate")
        ;&
    "init")
        ;&
    "run")
        take_lock $0
        trap "rm -f ${do_lockfile}" EXIT
        ;;
    esac

case "${1:-"usage"}" in
    "install")
        generate

        # Store current environment variables.
        (
            echo "Environment:"
            echo "============"
            env
            echo
        ) > ${installer_log_file}

        # Run installer and store logs.
        (
            echo "Installer logs:"
            echo "==============="
        ) >> ${installer_log_file}
        install 2>&1 | tee -a ${installer_log_file}
        ;;
    "generate")
        generate
        ;;
    "init")
        init_ovn_fake_multinode
        pull_ovn_fake_multinode
        ;;
    "run")
        cmd=$0
        shift
        test_file=$1
        out_dir=$2
        if [ -z "${test_file}" ]; then
            echo "Please supply a test scenario as argument!"
            usage ${cmd}
        fi
        if [ -z "${out_dir}" ]; then
            echo "Please supply an output results directory!"
            usage ${cmd}
        fi
        test_file=$(pwd)/${test_file}
        tstamp=$(date "+%Y%m%d-%H%M%S")
        out_dir=${result_dir}/${out_dir}-${tstamp}

        if [ ! -f ${test_file} ]; then
            echo "Test scenario ${test_file} does not exist!"
            usage ${cmd}
        fi
        if [ -d ${out_dir} ]; then
            echo "Results directory ${out_dir} already exists!"
            usage ${cmd}
        fi
        shift; shift

        test_file=$(translate_yaml ${test_file})
        # Run the new test.
        mkdir -p ${out_dir}
        run_test ${test_file} ${out_dir} 2>&1 | tee ${out_dir}/test-log
        ;;
    *)
        usage $0
        ;;
    esac

exit 0
