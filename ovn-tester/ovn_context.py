import ovn_stats

active_context = None


class Context(object):

    def __init__(self, test_name, max_iterations=1):
        self.iteration = -1
        self.test_name = test_name
        self.max_iterations = max_iterations

    def __enter__(self):
        global active_context
        print("***** Entering context {} *****".format(self.test_name))
        ovn_stats.clear()
        active_context = self
        return self

    def __exit__(self, type, value, traceback):
        ovn_stats.report(self.test_name)
        print("***** Exiting context {} *****".format(self.test_name))
        pass

    def __iter__(self):
        return self

    def __next__(self):
        if self.iteration < self.max_iterations - 1:
            self.iteration += 1
            return self.iteration
        raise StopIteration
