#!/usr/bin/env python3
"""Run a command on one available CPU with a single-thread solver profile."""
import json
import os
import sys


def main():
    source_path, derived_path, *command = sys.argv[1:]
    with open(source_path) as source:
        profile = json.load(source)
    for solver in profile['solvers']:
        solver['options']['thread_limit'] = 1
    with open(derived_path, 'w') as output:
        json.dump(profile, output)
    os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    os.execvp(command[0], command)


if __name__ == '__main__':
    main()
