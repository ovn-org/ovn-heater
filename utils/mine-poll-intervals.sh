#!/bin/bash

function mine_data_for_poll_intervals {
    res=$(echo "$1" \
        | grep 'Unreasonably' | cut -c 45- \
        | sed 's/.*Unreasonably long \([0-9]*\)ms .*/\1/' | sort -h \
        | datamash count 1 min 1 max 1 median 1 mean 1 perc 1 \
                   -t ' '  --format="%10.1f" )
    if [ -z "$res" ]; then
        printf "%10.1f\n" 0
    else
        echo "$res"
    fi
}

n_files=$#
echo
echo "Unreasonably long poll intervals:"
echo
echo "---------------------------------------------------------------------"
echo "     Count      Min        Max       Median      Mean   95 percentile"
echo "---------------------------------------------------------------------"

for file in "$@"; do
    mine_data_for_poll_intervals "$(cat $file)"
done

if test ${n_files} -gt 1; then
    echo "---------------------------------------------------------------------"
    mine_data_for_poll_intervals "$(cat $@)"
fi
echo
