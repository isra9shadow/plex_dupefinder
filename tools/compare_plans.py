#!/usr/bin/env python3
"""Compare two discovery plan files and report keeper / decision changes.

Use it to validate a scoring change on your REAL library without acting:

    1. Before the change, you already have a plan from an audit run, e.g.
       plans/dupefinder_plan_<old>.json
    2. After the change, run an audit again (AUDIT_MODE=true, CONFIRM_BEFORE_ACTION=false)
       to produce plans/dupefinder_plan_<new>.json
    3. python tools/compare_plans.py plans/<old>.json plans/<new>.json

It reads the plans only — it never touches Plex or any file. Groups are matched
by Plex item_key; it reports where the chosen keeper changed, where a group
became (un)actionable, and a summary.
"""
import argparse
import json
import os
import sys


def load_groups(path):
    with open(path, 'r', encoding='utf-8') as fp:
        plan = json.load(fp)
    by_key = {}
    for g in plan.get('groups', []):
        key = g.get('item_key')
        if key is not None:
            by_key[key] = g
    return plan, by_key


def keeper_files(group):
    """Map media_id -> filename(s) for readable before/after output."""
    out = {}
    for p in group.get('parts', []):
        out[p.get('media_id')] = p.get('files') or p.get('file')
    return out


def main():
    ap = argparse.ArgumentParser(description="Diff two dupefinder plan files.")
    ap.add_argument('old_plan')
    ap.add_argument('new_plan')
    ap.add_argument('--limit', type=int, default=50,
                    help="max changed groups to print (default 50)")
    args = ap.parse_args()

    for p in (args.old_plan, args.new_plan):
        if not os.path.isfile(p):
            print("Not found: %s" % p)
            sys.exit(2)

    _, old = load_groups(args.old_plan)
    _, new = load_groups(args.new_plan)

    common = sorted(set(old) & set(new))
    keeper_changed, skip_changed = [], []

    for key in common:
        od, nd = old[key]['decision'], new[key]['decision']
        title = new[key].get('title') or old[key].get('title') or key
        if bool(od.get('skip')) != bool(nd.get('skip')):
            skip_changed.append((title, bool(od.get('skip')), bool(nd.get('skip')),
                                 nd.get('skip_reason') or od.get('skip_reason')))
        elif od.get('keeper_id') != nd.get('keeper_id'):
            files = keeper_files(new[key])
            keeper_changed.append((title, od.get('keeper_id'), nd.get('keeper_id'),
                                   files.get(nd.get('keeper_id'))))

    print("Groups: old=%d new=%d common=%d" % (len(old), len(new), len(common)))
    print("Keeper changed : %d" % len(keeper_changed))
    print("Skip changed   : %d" % len(skip_changed))
    print("Only in old    : %d | Only in new: %d"
          % (len(set(old) - set(new)), len(set(new) - set(old))))

    if keeper_changed:
        print("\n--- KEEPER CHANGED (top %d) ---" % args.limit)
        for title, oldk, newk, files in keeper_changed[:args.limit]:
            print("  %s\n      keeper %s -> %s  %s" % (title, oldk, newk, files))

    if skip_changed:
        print("\n--- SKIP STATUS CHANGED (top %d) ---" % args.limit)
        for title, was, now, reason in skip_changed[:args.limit]:
            verb = "now SKIPPED" if now else "now ACTIONABLE"
            print("  %s\n      %s (%s)" % (title, verb, reason))


if __name__ == "__main__":
    main()
