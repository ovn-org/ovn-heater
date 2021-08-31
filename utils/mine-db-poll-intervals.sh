#!/bin/bash

function mine_data_for_poll_intervals {
    res=$(echo "$1" \
        | sed '/\(writes\|involuntary\)/s/$/\n\n/' \
        | egrep -A 2 -B 1 'Unreasonably' \
        | grep -v '\-\-' | cut -c 45- | paste -d " " - - - - \
        | sed 's/.*took \([0-9]*\)ms .*long \([0-9]*\)ms .* \([0-9]*\)ms system.* \([0-9]*\) writes.*/\2 \3 \4 compaction \1/' \
        | sed 's/.*long \([0-9]*\)ms .* \([0-9]*\)ms system.*/\1 \2/' \
        | sort -h | grep -v compaction | cut -f 1 -d ' ' \
        | datamash count 1 min 1 max 1 median 1 mean 1 perc 1 \
                   -t ' '  --format="%10.1f" )
    if [ -z "$res" ]; then
        printf "%10.1f\n" 0
    else
        echo "$res"
    fi
}

function mine_data_for_compaction {
    res=$(echo "$1" \
        | grep 'compaction took' | cut -c 43- \
        | sed 's/.*Database compaction took \(.*\)ms/\1/' | sort -h \
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
echo "Unreasonably long poll intervals that didn't involve database compaction:"
echo
echo "Note:"
echo "It's not possible to exclude compactions under 1 second long, so these,"
echo "if any, are accounted in below statistics.  Mistakes are also possible."
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

echo "Database compaction:"
echo
echo "Note: Compactions under 1 second long are not counted."
echo
echo "---------------------------------------------------------------------"
echo "     Count      Min        Max       Median      Mean   95 percentile"
echo "---------------------------------------------------------------------"

for file in "$@"; do
    mine_data_for_compaction "$(cat $file)"
done

if test ${n_files} -gt 1; then
    echo "---------------------------------------------------------------------"
    mine_data_for_compaction "$(cat $@)"
fi
echo
