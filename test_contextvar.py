import concurrent.futures
from contextvars import ContextVar

var = ContextVar('var', default=None)

def set_var(val):
    var.set(val)

def get_var():
    return var.get()

def worker(val):
    set_var(val)
    return get_var()

with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    results = list(executor.map(worker, [1, 2, 3]))

print("Results:", results)