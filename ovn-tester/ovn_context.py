import logging
import ovn_stats
import time
import asyncio

log = logging.getLogger(__name__)

active_context = None


ITERATION_STAT_NAME = 'Iteration Total'


class Context(object):
    def __init__(self, test_name, max_iterations=None, brief_report=False,
                 test=None):
        self.iteration = -1
        self.test_name = test_name
        self.max_iterations = max_iterations
        self.brief_report = brief_report
        self.iteration_start = None
        self.failed = False
        self.test = test
        self.iterations = dict()
        if self.max_iterations is None:
            self.iteration_singleton = ContextIteration(0, self)
        else:
            self.iteration_singleton = None

    def __enter__(self):
        global active_context
        log.info(f'Entering context {self.test_name}')
        ovn_stats.clear()
        active_context = self
        return self

    def __exit__(self, type, value, traceback):
        ovn_stats.report(self.test_name, brief=self.brief_report)
        log.info(f'Exiting context {self.test_name}')

    def __iter__(self):
        return self

    def __next__(self):
        now = time.perf_counter()
        if self.iteration_start:
            duration = now - self.iteration_start
            ovn_stats.add(ITERATION_STAT_NAME, duration, failed=self.failed)
            log.log(logging.WARNING if self.failed else logging.INFO,
                    f'Context {self.test_name}, Iteration {self.iteration}, '
                    f'Result: {"FAILURE" if self.failed else "SUCCESS"}')
        self.failed = False
        if self.test:
            # exec external cmd
            # self.test.exec_cmd(self.iteration, self.test_name)
            pass
        self.iteration_start = now
        if self.iteration < self.max_iterations - 1:
            self.iteration += 1
            log.info(f'Context {self.test_name}, Iteration {self.iteration}')
            return self.iteration
        raise StopIteration

    def fail(self):
        self.failed = True

    def iteration_started(self, iteration):
        '''Explicitly begin iteration 'n'. This is necessary when running
        asynchronously since task starts and ends can overlap.'''
        iteration.start()
        log.info(f'Context {self.test_name}, Iteration {iteration.num}')

    def iteration_completed(self, iteration):
        ''' Explicitly end iteration 'n'. This is necessary when running
        asynchronously since task starts and ends can overlap.'''
        iteration.end()
        duration = iteration.end_time - iteration.start_time
        ovn_stats.add(ITERATION_STAT_NAME, duration, iteration.failed)
        self.iterations = {task_name: it for task_name, it in
                           self.iterations.items() if it != iteration}
        log.log(logging.WARNING if iteration.failed else logging.INFO,
                f'Context {self.test_name}, Iteration {iteration.num}, '
                f'Result: {"FAILURE" if iteration.failed else "SUCCESS"}')

    def create_task(self, coro, iteration=None):
        '''Create a task to run in this context.'''
        if iteration is None:
            iteration = get_current_iteration()
        task = asyncio.create_task(coro)
        self.iterations[task.get_name()] = iteration
        return task


def get_current_iteration():
    ctx = active_context
    if ctx.iteration_singleton is not None:
        return ctx.iteration_singleton

    cur_task = asyncio.current_task()
    return active_context.iterations[cur_task.get_name()]


class ContextIteration(object):
    def __init__(self, num, context):
        self.num = num
        self.context = context
        self.start_time = None
        self.end_time = None
        self.failed = False

    def fail(self):
        self.failed = True

    def start(self):
        self.start_time = time.perf_counter()

    def end(self):
        self.end_time = time.perf_counter()
