import json
import numpy
import os
import pandas as pd
import plotly.express as px
import sys
import time

from datetime import datetime


def read_file(filename):
    with open(filename, "r") as file:
        return json.load(file)

def resource_stats_generate(filename, data):
    rss = []
    cpu = []

    for ts, time_slice in sorted(data.items()):
        for name, res in time_slice.items():
            tme = datetime.fromtimestamp(float(ts))
            rss_mb = int(res['rss']) >> 20
            rss.append([tme, name, rss_mb])
            cpu.append([tme, name, float(res['cpu'])])

    df_rss = pd.DataFrame(rss, columns=['Time', 'Process', 'RSS (MB)'])
    df_cpu = pd.DataFrame(cpu, columns=['Time', 'Process', 'CPU (%)'])

    rss_chart = px.line(df_rss, x='Time', y='RSS (MB)', color='Process',
                        title='Resident Set Size')
    cpu_chart = px.line(df_cpu, x='Time', y='CPU (%)', color='Process',
                        title='CPU usage')

    with open(filename, 'w') as report_file:
        report_file.write('<html>')
        report_file.write(rss_chart.to_html(full_html=False,
                                            include_plotlyjs='cdn',
                                            default_width='90%',
                                            default_height='90%'))
        report_file.write(cpu_chart.to_html(full_html=False,
                                            include_plotlyjs='cdn',
                                            default_width='90%',
                                            default_height='90%'))
        report_file.write('</html>')


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(f'Usage: {sys.argv[0]} output-file input-file [input-file ...]')
        sys.exit(1)

    if os.path.isfile(sys.argv[1]):
        print(f'Output file {sys.argv[1]} already exists')
        sys.exit(2)

    data = {}
    for f in sys.argv[2:]:
        d = read_file(f)
        data.update(d)

    resource_stats_generate(sys.argv[1], data)
