#!/bin/bash

set -o errexit -o pipefail

if grep -B 30 -A 30 "Result: FAIL" ./test_results/*/test-log      \
        || grep -B 30 -A 30 "Traceback" ./test_results/*/test-log \
        || grep "Failed to run test" ./test_results/*/test-log; then
    exit 1
fi

if ! grep "Result: SUCCESS" ./test_results/*/test-log; then
    exit 1
fi

exit 0
