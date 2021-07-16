import collections
import functools
import numpy
import ovn_context
import ovn_exceptions
import pandas as pd
import plotly.express as px
import time

timed_functions = collections.defaultdict(list)


def timeit(func):
    @functools.wraps(func)
    def _timeit(*args, **kwargs):
        start = time.perf_counter()
        failed = False
        value = None
        try:
            value = func(*args, **kwargs)
        except ovn_exceptions.OvnTestException:
            failed = True
        finally:
            duration = time.perf_counter() - start
            add(func.__qualname__, duration, failed)
        return value
    return _timeit


def clear():
    global timed_functions
    timed_functions.clear()


def add(fname, duration, failed):
    if failed:
        ovn_context.active_context.fail()
    iteration = ovn_context.active_context.iteration
    elem = (duration, failed)
    timed_functions[(fname, iteration)].append(elem)


def report(test_name, brief=False):
    all_stats = collections.defaultdict(list)
    fail_stats = collections.defaultdict(list)
    chart_stats = collections.defaultdict(list)
    headings = [
        'Min (s)', 'Median (s)', '90%ile (s)', 'Max (s)', 'Mean (s)',
        'Total (s)', 'Count', 'Failed'
    ]
    for (f, i), measurements in timed_functions.items():
        for (d, r) in measurements:
            all_stats[f].append(d)
            chart_stats[f].append([f'{i}', f, d])
            if r:
                fail_stats[f].append(i)

    if len(all_stats.items()) == 0:
        return

    all_avgs = []
    all_f = []
    for f, measurements in sorted(all_stats.items()):
        all_avgs.append([numpy.min(measurements), numpy.median(measurements),
                         numpy.percentile(measurements, 90),
                         numpy.max(measurements),
                         numpy.mean(measurements),
                         numpy.sum(measurements),
                         len(measurements),
                         len(fail_stats[f])])
        all_f.append(f)

    df = pd.DataFrame(all_avgs, index=all_f, columns=headings)
    stats_html = df.to_html()

    with open(f'{test_name}-report.html', 'w') as report_file:
        report_file.write('<html>')
        report_file.write(stats_html)

        if brief:
            report_file.write('</html>')
            return

        for f, values in sorted(chart_stats.items()):
            df = pd.DataFrame(values,
                              columns=['Iteration', 'Counter', 'Value (s)'])
            chart = px.bar(df, x='Iteration', y='Value (s)', color='Counter',
                           title=f)
            chart.update_traces(marker_color='#005cb8', opacity=1.0,
                                marker_line_width=1.5,
                                marker_line_color='#005cb8')
            report_file.write(chart.to_html(full_html=False,
                                            include_plotlyjs='cdn',
                                            default_width='90%',
                                            default_height='90%'))

        report_file.write('</html>')
