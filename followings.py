#!/usr/bin/env python3
"""

A script that go through your followings and unfollows dead accounts.
It notices empty accounts, accounts that were deleted locally and remotely,
and also cleans up dead instances if allowed to.
It has a cache so you can run it once without --unfollow to preview its
actions, and a second time that will skip all verified active profiles.
With colors and a nice progress bar with item count, %, and ETA.

Requirements:
    pipenv install
    cp settings.py.example settings.py
    # then edit settings.py to have the correct stuff

Then run it:
    pipenv run python followings.py --help

Required API scopes:
    read:accounts read:follows read:statuses write:blocks write:follows
"""

from argparse import ArgumentParser, RawDescriptionHelpFormatter
from datetime import timedelta, datetime, timezone
import os
import pickle
import shutil

from mastodon import Mastodon
from tqdm import tqdm
import requests
import dateutil
import dateutil.parser
from colorama import Fore, Style, init as colorama_init

import settings


HTTP_TIMEOUT = 6


def cprint(c, s):
    print(c + s + Fore.WHITE + Style.NORMAL)


def parse_time_ago(v):
    v = v.lower().strip()
    units = {
        'y': timedelta(days=365),
        'm': timedelta(days=30),
        'd': timedelta(days=1),
    }
    return int(v[:-1]) * units[v[-1]]


class Error(Exception):
    pass


class UserGone(Exception):
    pass



def get_last_toot(mastodon, fid):

    if settings.CACHE_FILE:
        try:
            with open(settings.CACHE_FILE, 'rb') as f:
                cache = pickle.load(f)
        except Exception as e:
            cprint(Fore.RED, "Error loading cache: {}".format(e))
            cache = {}
    else:
        cache = {}

    if fid in cache:
        return cache[fid]

    statuses = mastodon.account_statuses(fid)

    if not statuses:
        raise UserGone("No toot found - New way")
    result = min(t.get('created_at') for t in statuses)

    if settings.CACHE_FILE:
        cache[fid] = result
        try:
            if os.path.isfile(settings.CACHE_FILE):
                shutil.copyfile(settings.CACHE_FILE, settings.CACHE_FILE + '.prev')
            with open(settings.CACHE_FILE, 'wb') as f:
                cache = pickle.dump(cache, f)
        except Exception as e:
            cprint(Fore.RED, "Error saving cache: {!r}".format(e))

    return result


def main():
    colorama_init()

    parser = ArgumentParser(description=__doc__,
                            formatter_class=RawDescriptionHelpFormatter)
    parser.add_argument('--min-activity', dest='min_activity',
                        type=parse_time_ago, default="1y",
                        help=("Remove followings inactive for a given period"
                              " (m for months, y for years, d for days) "
                              "(default: %(default)s)"))
    parser.add_argument('--target-count', dest='target_count', type=int,
                        help=("Target some following count (will try to stop"
                              " when you have that many followings left)"))
    parser.add_argument('--unfollow', action='store_true',
                        help="Actually unfollow")
    parser.add_argument('--followers', action='store_true',
                        help="Instead of removing people you follow, remove "
                        "people who follow YOU")
    parser.add_argument('--unmutuals', action='store_true',
                        help="Remove people who follow you but that you "
                        "don't follow")
    parser.add_argument('-v', '--verbose', action='store_true',
                        help="Display more things")

    args = parser.parse_args()

    session = requests.Session()

    mastodon = Mastodon(
        access_token=settings.ACCESS_TOKEN,
        api_base_url=settings.API_BASE,
        ratelimit_method='pace'
    )

    current_user = mastodon.account_verify_credentials()
    uid = current_user['id']

    if args.unmutuals:
        args.followers = True

    if args.followers:
        followings_count = current_user['followers_count']
    else:
        followings_count = current_user['following_count']
    local_count = followings_count

    goal_msg = ""
    if args.target_count:
        goal_msg = "(goal: n>={})".format(args.target_count)

    now = datetime.now(tz=timezone.utc)

    def clog(c, s):
        tqdm.write(c + s + Fore.WHITE + Style.NORMAL)

    cprint(Fore.GREEN, "Current user: @{} (#{})".format(current_user['username'], uid))
    cprint(Fore.GREEN, "{}: {} {}".format(
            "Followers" if args.followers else "Followings",
            followings_count, goal_msg))

    if args.unfollow:
        cprint(Fore.RED, "Action: unfollow")
    else:
        cprint(Fore.YELLOW, "Action: none")

    followings = None
    if args.followers:
        followings = mastodon.account_followers(uid)
    else:
        followings = mastodon.account_following(uid)
    followings = mastodon.fetch_remaining(followings)

    bar = tqdm(list(followings))
    for f in bar:
        fid = f.get('id')
        acct = f.get('acct')
        fullhandle = "@{}".format(acct)

        if '@' in acct:
            inst = acct.split('@', 1)[1].lower()
        else:
            inst = None

        if args.target_count is not None and local_count <= args.target_count:
            clog(Fore.RED + Style.BRIGHT,
                 "{} followings left; stopping".format(local_count))
            break

        title_printed = False

        def title():
            nonlocal title_printed
            if title_printed:
                return
            title_printed = True
            clog(Fore.WHITE + Style.BRIGHT,
                 "Account: {} (#{})".format(f.get('acct'), fid))

        if args.verbose:
            title()
        try:
            bar.set_description(fullhandle.ljust(30, ' '))

            act = False

            if args.unmutuals and inst not in settings.SKIP_INSTANCES:
                try:
                    relations = mastodon.account_relationships(fid)
                    is_mutual = relations[0]["following"] or relations[0]["requested"]

                    if not is_mutual:
                        clog(Fore.YELLOW, "- Unmutual ({})".format(relations))
                        act = True
                except (UserGone, Error, requests.RequestException) as e:
                    act = False
                    clog(Fore.YELLOW, "- Exception ({})".format(e))

            elif args.min_activity and inst not in settings.SKIP_INSTANCES:
                try:
                    print(f.get('url'))
                    last_toot = get_last_toot(mastodon, fid)
                    if last_toot < now - args.min_activity:
                        act = True
                        msg = "(!)"
                        title()
                        clog(Fore.WHITE, "- Last toot: {} {}".format(last_toot, msg))
                    else:
                        msg = "(pass)"
                        if args.verbose:
                            clog(Fore.WHITE, "- Last toot: {} {}".format(last_toot, msg))
                except UserGone as e:
                    moved = f.get('moved')
                    if moved:
                        # TODO: follow new account and unfollow old
                        act = False
                        title()
                        clog(Fore.YELLOW,
                             "- User moved ({}) [NOT IMPLEMENTED]".format(moved))
                    else:
                        act = True
                        title()
                        clog(Fore.YELLOW, "- User gone ({})".format(e))
                except (Error, requests.RequestException) as e:
                    if inst and inst in settings.ASSUME_DEAD_INSTANCES:
                        act = True
                        title()
                        clog(Fore.YELLOW, "- Instance gone ({})".format(e))
                    else:
                        raise

            if act:
                local_count -= 1

                if args.unfollow:
                    if args.followers:
                        clog(Fore.GREEN + Style.BRIGHT,
                             "- Removing follower {}".format(fullhandle))
                        mastodon.account_block(fid)
                        mastodon.account_unblock(fid)
                    else:
                        clog(Fore.GREEN + Style.BRIGHT,
                             "- Unfollowing {}".format(fullhandle))
                        mastodon.account_unfollow(fid)
                else:
                    clog(Fore.GREEN + Style.BRIGHT,
                         "- (not) unfollowing {}".format(fullhandle))

                clog(Fore.WHITE, ("- {}/{} followings left"
                                  .format(local_count, followings_count)))
        except Exception as e:
            title()
            clog(Fore.RED, "- Error: {}".format(str(e)))


if __name__ == '__main__':
    main()
