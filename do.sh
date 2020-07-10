#!/bin/bash

set -o errexit

topdir=$(pwd)
rundir=${topdir}/runtime
deployment_dir=${topdir}/physical-deployments
result_dir=${topdir}/test_results

phys_deployment="${PHYS_DEPLOYMENT:-${deployment_dir}/physical-deployment.yml}"

browbeat_venv=${rundir}/browbeat/.browbeat-venv

rally_ovs_venv=${rundir}/browbeat/.rally-ovs-venv
deployment="${RALLY_DEPLOYMENT:-ovn-fake-multinode}"
deployment_file=${rundir}/deployment.json

rally_utils=${topdir}/ovn-scale-test-utils
rally_deployment_generate=${rally_utils}/generate-deployment.py
clustered_db=${CLUSTERED_DB:-True}

ovn_fmn_utils=${topdir}/ovn-fake-multinode-utils
ovn_fmn_playbooks=${ovn_fmn_utils}/playbooks
ovn_fmn_generate=${ovn_fmn_utils}/generate-hosts.py
ovn_fmn_docker=${ovn_fmn_utils}/generate-docker-cfg.py
hosts_file=${rundir}/hosts
installer_log_file=${rundir}/installer-log
docker_daemon_file=${rundir}/docker-daemon.json
log_collector_file=${rundir}/log-collector.sh
log_perf_file=${rundir}/perf.sh

EXTRA_OPTIMIZE=${EXTRA_OPTIMIZE:-no}

function generate() {
    # Make sure rundir exists.
    mkdir -p ${rundir}

    PYTHONPATH=${topdir}/utils ${ovn_fmn_generate} ${phys_deployment} ${rundir} ${ovn_fmn_repo} ${ovn_fmn_branch} > ${hosts_file}
    PYTHONPATH=${topdir}/utils ${ovn_fmn_docker} ${phys_deployment} > ${docker_daemon_file}
    PYTHONPATH=${topdir}/utils ${rally_deployment_generate} ${phys_deployment} ${clustered_db}> ${deployment_file}
    cp ${ovn_fmn_utils}/scripts/log-collector.sh ${log_collector_file}
    cp ${ovn_fmn_utils}/scripts/perf.sh ${log_perf_file}
}

function install_deps() {
    echo "-- Installing dependencies on all nodes"
    ansible-playbook ${ovn_fmn_playbooks}/install-dependencies.yml -i ${hosts_file}

    echo "-- Installing local dependencies"
    dnf install -y docker docker-distribution
    systemctl start docker
    systemctl start docker-distribution
}

function configure_docker() {
    echo "-- Configuring docker local registry on all nodes"
    ansible-playbook ${ovn_fmn_playbooks}/configure-docker-registry.yml -i ${hosts_file}
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
        git clone ${comp_repo} ${comp_name}
        pushd ${comp_name}
        git checkout ${comp_branch}
        popd
        return 1
    fi
    popd
}

# browbeat env vars
#TODO: right now we clone a fork. Do we try to get the changes in master?
browbeat_repo="${BROWBEAT_REPO:-https://github.com/dceara/browbeat.git}"
browbeat_branch="${BROWBEAT_BRANCH:-master}"

function install_browbeat() {
    if clone_component browbeat ${browbeat_repo} ${browbeat_branch}; then
        return 0
    fi

    pushd ${rundir}/browbeat
    virtualenv ${browbeat_venv}
    source ${browbeat_venv}/bin/activate
    pip install -r requirements.txt
    deactivate
    popd
}

# rally env vars
#TODO: right now we clone a fork. Do we try to get the changes in master?
rally_repo="${RALLY_REPO:-https://github.com/dceara/rally.git}"
rally_branch="${RALLY_BRANCH:-master}"

function install_rally() {
    if clone_component rally ${rally_repo} ${rally_branch}; then
        return 0
    fi

    pushd ${rundir}/rally
    ./install_rally.sh -y -d ${rally_ovs_venv}
    popd
}

# rally-ovs env vars
#TODO: right now we clone a fork. Do we try to get the changes in master?
rally_ovs_repo="${RALLY_OVS_REPO:-https://github.com/dceara/ovn-scale-test.git}"
rally_ovs_branch="${RALLY_OVS_BRANCH:-ovn-switch-per-node}"

function install_rally_ovs() {
    if clone_component ovn-scale-test ${rally_ovs_repo} ${rally_ovs_branch}; then
        return 0
    fi

    pushd ${rundir}/ovn-scale-test
    git checkout ${rally_ovs_branch}
    ./install.sh -y -d ${rundir}/browbeat/.rally-ovs-venv
    popd
}

# OVS/OVN env vars
ovs_repo="${OVS_REPO:-https://github.com/openvswitch/ovs.git}"
ovs_branch="${OVS_BRANCH:-v2.13.0}"
ovn_repo="${OVN_REPO:-https://github.com/ovn-org/ovn.git}"
ovn_branch="${OVN_BRANCH:-v20.03.0}"

# ovn-fake-multinode env vars
ovn_fmn_repo="${OVN_FAKE_MULTINODE_REPO:-https://github.com/ovn-org/ovn-fake-multinode.git}"
ovn_fmn_branch="${OVN_FAKE_MULTINODE_BRANCH:-master}"

function install_ovn_fake_multinode() {
    echo "-- Cloning ${ovn_fmn_repo} on all nodes, revision ${ovn_fmn_branch}"
    # Clone repo on all hosts.
    ansible-playbook ${ovn_fmn_playbooks}/install-fake-multinode.yml -i ${hosts_file}

    local rebuild_needed=0

    # Clone repo locally
    clone_component ovn-fake-multinode ${ovn_fmn_repo} ${ovn_fmn_branch} || rebuild_needed=1
    clone_component ovs ${ovs_repo} ${ovs_branch} || rebuild_needed=1
    clone_component ovn ${ovn_repo} ${ovn_branch} || rebuild_needed=1

    pushd ${rundir}/ovn-fake-multinode
    if [ ${rebuild_needed} -eq 1 ]; then
        # Build images locally.
        OVS_SRC_PATH=${rundir}/ovs OVN_SRC_PATH=${rundir}/ovn EXTRA_OPTIMIZE=${EXTRA_OPTIMIZE} ./ovn_cluster.sh build
    fi
    # Tag and push image
    docker tag ovn/ovn-multi-node localhost:5000/ovn/ovn-multi-node
    docker push localhost:5000/ovn/ovn-multi-node
    popd
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

function install() {
    pushd ${rundir}
    install_deps
    configure_docker
    install_browbeat
    install_rally
    install_rally_ovs
    install_ovn_fake_multinode
    init_ovn_fake_multinode
    pull_ovn_fake_multinode
    popd
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

    components=("browbeat" "rally" "ovn-scale-test" "ovn-fake-multinode" "ovs" "ovn")
    for d in "${components[@]}"; do
        pushd ${rundir}/$d
        local origin=$(git config --get remote.origin.url)
        local sha=$(git rev-parse HEAD)
        local sha_name=$(git rev-parse --abbrev-ref HEAD)
        echo "$d (${origin}): ${sha} (${sha_name})" >> ${out_file}
        popd
    done
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

    pushd ${rundir}/browbeat
    source ${browbeat_venv}/bin/activate
    python browbeat.py -s ${test_file} --debug

    echo "-- Moving results and logs to: ${out_dir}"
    mv results/* ${out_dir}
    ansible-playbook ${ovn_fmn_playbooks}/collect-logs.yml -i ${hosts_file} --extra-vars "results_dir=${out_dir}/logs"

    pushd ${out_dir}/logs
    for f in *.tgz; do
        tar xvfz $f
    done
    popd

    deactivate
    popd
}

function usage() {
    echo "Usage: $0 install|generate|rally-deploy|rally-undeploy|init|browbeat-run <scenario> <out-dir>"
    exit 1
}

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
    "rally-deploy")
        source ${rally_ovs_venv}/bin/activate
        rally-ovs deployment create --file ${deployment_file} --name ${deployment}
        deactivate
        ;;
    "rally-undeploy")
        source ${rally_ovs_venv}/bin/activate
        rally-ovs deployment destroy ${deployment}
        deactivate
        ;;
    "init")
        init_ovn_fake_multinode
        pull_ovn_fake_multinode
        ;;
    "browbeat-run")
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

        run_test ${test_file} ${out_dir}
        ;;
    *)
        usage $0
        ;;
    esac

exit 0
