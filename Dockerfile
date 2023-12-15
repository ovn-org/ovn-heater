FROM ovn/ovn-multi-node

ARG SSH_KEY

COPY ovn-tester /ovn-tester

RUN mkdir -p /root/.ssh/
COPY $SSH_KEY /root/.ssh/

COPY ovn-fake-multinode-utils/process-monitor.py /tmp/

# This variable is needed on systems where global python's
# environment is marked as "Externally managed" (PEP 668) to allow pip
# installation of "global" packages.
ENV PIP_BREAK_SYSTEM_PACKAGES=1
RUN pip3 install -r /ovn-tester/requirements.txt
