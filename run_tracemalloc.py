import runpy
import tracemalloc
import linecache
import sys
import time

def display_top(snapshot, key_type='lineno', limit=10):
    snapshot = snapshot.filter_traces((
        tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
        tracemalloc.Filter(False, "<unknown>"),
    ))
    top_stats = snapshot.statistics(key_type)

    print("Top %s lines" % limit)
    for index, stat in enumerate(top_stats[:limit], 1):
        frame = stat.traceback[0]
        print("#%s: %s:%s: %.1f KiB"
              % (index, frame.filename, frame.lineno, stat.size / 1024))
        line = linecache.getline(frame.filename, frame.lineno).strip()
        if line:
            print('    %s' % line)

    other = top_stats[limit:]
    if other:
        size = sum(stat.size for stat in other)
        print("%s other: %.1f KiB" % (len(other), size / 1024))
    total = sum(stat.size for stat in top_stats)
    print("Total allocated size: %.1f KiB" % (total / 1024))

def take_snapshot(label):
    snapshot = tracemalloc.take_snapshot()
    print(f"\nSnapshot at {label}:")
    display_top(snapshot)

if __name__ == "__main__":
    module_name = 'streamgram'

    # Start tracemalloc
    tracemalloc.start()

    # Take an initial snapshot
    take_snapshot("start")

    # Run the module
    runpy.run_module(module_name, run_name="__main__")

    # Take a snapshot after running the module
    take_snapshot("end")
    
    # Stop tracemalloc
    tracemalloc.stop()
