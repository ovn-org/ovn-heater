import collections
import functools
import numpy
import ovn_context
import pandas as pd
import plotly.express as px
import time

timed_functions = collections.defaultdict(list)


def timeit(func):
    @functools.wraps(func)
    def _timeit(*args, **kwargs):
        start = time.perf_counter()
        value = func(*args, **kwargs)
        duration = time.perf_counter() - start
        add(func.__name__, duration)
        return value
    return _timeit


def clear():
    global timed_functions
    timed_functions.clear()


def add(fname, duration):
    iteration = ovn_context.active_context.iteration
    timed_functions[(fname, iteration)].append(duration)


def report(test_name):
    all_stats = collections.defaultdict(list)
    chart_stats = collections.defaultdict(list)
    headings = [
        "Min (s)", "Median (s)", "90%%ile (s)", "Max (s)", "Mean (s)", "Count"
    ]
    for (f, i), measurements in timed_functions.items():
        all_stats[f].extend(measurements)
        chart_stats[f].extend(
            [['Iteration {}'.format(i), f, m] for m in measurements]
        )

    if len(all_stats.items()) == 0:
        return

    all_avgs = []
    all_f = []
    for f, measurements in sorted(all_stats.items()):
        all_avgs.append([numpy.min(measurements), numpy.median(measurements),
                         numpy.percentile(measurements, 90),
                         numpy.max(measurements),
                         numpy.mean(measurements),
                         len(measurements)])
        all_f.append(f)

    df = pd.DataFrame(all_avgs, index=all_f, columns=headings)
    stats_html = df.to_html()

    with open("{}-report.html".format(test_name), "w") as report_file:
        report_file.write("<html>")
        report_file.write(stats_html)

        for f, values in sorted(chart_stats.items()):
            df = pd.DataFrame(values,
                              columns=['Iteration', 'Counter', 'Value (s)'])
            chart = px.bar(df, x='Iteration', y='Value (s)', color='Counter',
                           title=f)
            report_file.write(chart.to_html(full_html=False,
                                            include_plotlyjs='cdn',
                                            default_width='50%',
                                            default_height='50%'))

        report_file.write("</html>")
