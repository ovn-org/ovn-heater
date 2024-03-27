import argparse
import json
import logging
import os
import pandas as pd
import plotly.express as px
import sys

from typing import Dict, List


FORMAT = '%(asctime)s |%(levelname)s| %(message)s'
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format=FORMAT)
log = logging.getLogger(__name__)


def read_file(filename: str) -> Dict:
    with open(filename, "r") as file:
        return json.load(file)


def aggregated(df: pd.DataFrame) -> (pd.DataFrame, int):
    column_names = list(df.columns)
    value_name = column_names[2]

    log.info(f'Pivot and interpolate {value_name} ...')
    df = df.pivot_table(
        index='Time', columns='Process', values=value_name, aggfunc='mean'
    ).interpolate(method='time', limit_direction='both')

    result = pd.DataFrame(index=df.index)
    processes = {p.split('|')[0] for p in df.columns}

    log.info(f'Aggregating {value_name} ...')
    for p in processes:
        df_filtered = df.filter(regex='^' + p)
        result[p + '|sum'] = df_filtered.sum(axis=1)
        result[p + '|mean'] = df_filtered.mean(axis=1)
        result[p + '|max'] = df_filtered.max(axis=1)
        result[p + '|min'] = df_filtered.min(axis=1)

    result['ovn|sum'] = df.filter(regex=r'^ovn.*\|ovn-(central|scale).*').sum(
        axis=1
    )
    ovn_max = result['ovn|sum'].astype('int').max()

    result['ovs|sum'] = df.filter(regex=r'^ovs.*\|ovn-(central|scale).*').sum(
        axis=1
    )

    result = result.astype('int').reset_index().melt(id_vars=['Time'])
    result.columns = column_names
    result = result.sort_values(['Process', 'Time'])

    return result, ovn_max


def resource_stats_generate(
    filename: str, data: Dict, aggregate: bool
) -> None:
    rss: List[List] = []
    cpu: List[List] = []

    log.info('Preprocessing ...')
    for ts, time_slice in sorted(data.items()):
        tme = pd.Timestamp.fromtimestamp(float(ts)).round('1s')
        for name, res in time_slice.items():
            rss_mb = int(res['rss']) >> 20
            rss.append([tme, name, rss_mb])
            cpu.append([tme, name, float(res['cpu'])])

    log.info('Creating DataFrame ...')
    df_rss = pd.DataFrame(rss, columns=['Time', 'Process', 'RSS (MB)'])
    df_cpu = pd.DataFrame(cpu, columns=['Time', 'Process', 'CPU (%)'])

    if aggregate:
        df_rss, max_sum_rss = aggregated(df_rss)
        df_cpu, max_sum_cpu = aggregated(df_cpu)

    log.info('Creating charts ...')
    rss_chart = px.line(
        df_rss,
        x='Time',
        y='RSS (MB)',
        color='Process',
        title=('Aggregate ' if aggregate else '') + 'Resident Set Size',
    )
    cpu_chart = px.line(
        df_cpu,
        x='Time',
        y='CPU (%)',
        color='Process',
        title=('Aggregate ' if aggregate else '') + 'CPU usage',
    )

    log.info(f'Writing HTML to {filename} ...')
    with open(filename, 'w') as report_file:
        report_file.write('<html>')
        if aggregate:
            report_file.write(
                f'''
                <table border="1" class="dataframe">
                <tbody>
                    <tr>
                        <td>Max(Sum(OVN RSS))</td>
                        <td> {max_sum_rss} MB</td>
                    </tr>
                    <tr>
                        <td>Max(Sum(OVN CPU))</td>
                        <td> {max_sum_cpu} %</td>
                    </tr>
                </tbody>
                </table>
            '''
            )
        report_file.write(
            rss_chart.to_html(
                full_html=False,
                include_plotlyjs='cdn',
                default_width='90%',
                default_height='90%',
            )
        )
        report_file.write(
            cpu_chart.to_html(
                full_html=False,
                include_plotlyjs='cdn',
                default_width='90%',
                default_height='90%',
            )
        )
        report_file.write('</html>')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Generate resource usage charts.'
    )
    parser.add_argument(
        '--aggregate', action='store_true', help='generate aggregate charts'
    )
    parser.add_argument(
        '-o', '--output', required=True, help='file to write an HTML result'
    )
    parser.add_argument(
        'input_files',
        metavar='input-file',
        type=str,
        nargs='+',
        help='JSON file with recorded process statistics',
    )

    args = parser.parse_args()

    if os.path.isfile(args.output):
        log.fatal(f'Output file {args.output} already exists')
        sys.exit(2)

    log.info(f'Processing stats from {len(args.input_files)} files.')

    log.info('Reading ...')
    data: Dict = {}
    for f in args.input_files:
        d = read_file(f)
        data.update(d)

    resource_stats_generate(args.output, data, args.aggregate)
    log.info('Done.')
