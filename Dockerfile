FROM ovn/ovn-multi-node

ARG SSH_KEY
ARG PHYS_DEPLOYMENT

COPY ovn-tester /ovn-tester

RUN mkdir -p /root/.ssh/
COPY $SSH_KEY /root/.ssh/

COPY $PHYS_DEPLOYMENT /physical-deployment.yml
COPY ovn-fake-multinode-utils/process-monitor.py /tmp/

RUN pip3 install -r /ovn-tester/requirements.txt
