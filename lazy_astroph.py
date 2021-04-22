#!/usr/bin/env python3

from __future__ import print_function

import argparse
import datetime as dt
import json
import os
import platform
import shlex
import smtplib
import subprocess
import sys
from email.mime.text import MIMEText

import feedparser

# python 2 and 3 do different things with urllib
try:
    from urllib.request import urlopen
except ImportError:
    from urllib2 import urlopen

ArxivCategoryMap = {"astro-ph": ["GA", "CO", "EP", "HE", "IM", "SR"],
                    "cond-mat": ["dis-nn", "mes-hall", "mtrl-sci", "other", "quant-gas", "soft", "stat-mech", "str-el", "supr-con"],
                    "gr-qc": [""],
                    "hep-ex": [""],
                    "hep-lat": [""],
                    "hep-ph": [""],
                    "hep-th": [""],
                    "math-ph": [""],
                    "nlin": ["AO", "CD", "CG", "PS", "SI"],
                    "nucl-ex": [""],
                    "nucl-th": [""],
                    "physics": ["acc-ph", "ao-ph", "app-ph", "atm-clus", "atom-ph", "bio-ph", "chem-ph", "class-ph", "comp-ph", "data-an", "ed-ph", "flu-dyn", "gen-ph", "geo-ph", "hist-ph", "ins-det", "med-ph", "optics", "plasm-ph", "pop-ph", "soc-ph", "space-ph"],
                    "quant-ph": [""]}

def ArxivCategoryIterator(category):
    # takes a category and yields each "{category}.{subcategory}" string inside it
    if not category in ArxivCategoryMap:
        # this will catch cases where category includes the subcategory, e.g. "astro-ph.GA"
        yield category
    else:
        for subcategory in ArxivCategoryMap[category]:
            if subcategory:
                # this will catch cases where the category has multiple subcategories
                yield "{}.{}".format(category, subcategory)
            else:
                # this will catch cases where the category has no subcategories
                yield category

class Paper:
    """a Paper is a single paper listed on arXiv.  In addition to the
       paper's title, ID, and URL (obtained from arXiv), we also store
       which keywords it matched and which Slack channel it should go
       to"""

    def __init__(self, arxiv_id, title, url, keywords, channels):
        self.arxiv_id = arxiv_id
        self.title = title.replace("'", r"")
        self.url = url
        self.keywords = list(keywords)
        self.channels = list(set(channels))
        self.posted_to_slack = 0

    def __eq__(self, other):
        return self.arxiv_id == other.arxiv_id

    def __hash__(self):
        return hash(self.arxiv_id)

    def __str__(self):
        t = " ".join(self.title.split())  # remove extra spaces
        return u"{} : {}\n  {}\n".format(self.arxiv_id, t, self.url)

    def kw_str(self):
        """ return the union of keywords """
        return ", ".join(self.keywords)

    def __lt__(self, other):
        """we compare Papers by the number of keywords, and then
           alphabetically by the union of their keywords"""

        if len(self.keywords) == len(other.keywords):
            return self.kw_str() < other.kw_str()

        return len(self.keywords) < len(other.keywords)


class Keyword:
    """a Keyword includes: the text we should match, how the matching
       should be done (unique or any), which words, if present, negate
       the match, and what Slack channel this keyword is associated with"""

    def __init__(self, name, matching="any", required=False, channel=None, excludes=None):
        self.name = name
        self.matching = matching
        self.required = required
        self.channel = channel
        self.excludes = list(set(excludes))

    def __str__(self):
        return "{}: matching={}, channel={}, NOTs={}".format(
            self.name, self.matching, self.channel, self.excludes)


class ArxivQuery:
    """ a class to define a query to arXiv papers """

    def __init__(self, start_date, end_date, max_papers, old_id=None,
                 category="astro-ph"):
        self.start_date = start_date
        self.end_date = end_date
        self.max_papers = max_papers
        self.old_id = old_id

        self.base_url = "http://export.arxiv.org/api/query?"
        self.sort_query = "max_results={}&sortBy=submittedDate&sortOrder=descending".format(
            self.max_papers)

        self.category = category
        self.categories = list(ArxivCategoryIterator(self.category))

    def get_cat_query(self):
        """ create the category portion of the arxiv query """

        cat_query = "%28"  # open parenthesis
        for n, s in enumerate(self.categories):
            cat_query += s
            if n < len(self.categories)-1:
                cat_query += "+OR+"
            else:
                cat_query += "%29"  # close parenthesis

        return cat_query

    def get_range_query(self):
        """ get the query string for the date range """

        # here the 2000 on each date is 8:00pm
        range_str = "[{}2000+TO+{}2000]".format(self.start_date.strftime("%Y%m%d"),
                                                self.end_date.strftime("%Y%m%d"))
        range_query = "lastUpdatedDate:{}".format(range_str)
        return range_query

    def get_url(self):
        """ create the URL we will use to query arXiv """

        cat_query = self.get_cat_query()
        range_query = self.get_range_query()

        full_query = "search_query={}+AND+{}&{}".format(cat_query, range_query, self.sort_query)

        return self.base_url + full_query

    def do_query(self, keywords=None, old_id=None):
        """ perform the actual query """

        # note, in python3 this will be bytes not str
        response = urlopen(self.get_url()).read()
        response = response.replace(b"author", b"contributor")

        # this feedparser magic comes from the example of Julius Lucks / Andrea Zonca
        # https://github.com/zonca/python-parse-arxiv/blob/master/python_arXiv_parsing_example.py
        #feedparser._FeedParserMixin.namespaces['http://a9.com/-/spec/opensearch/1.1/'] = 'opensearch'
        #feedparser._FeedParserMixin.namespaces['http://arxiv.org/schemas/atom'] = 'arxiv'

        feed = feedparser.parse(response)

        if feed.feed.opensearch_totalresults == 0:
            sys.exit("no results found")

        results = []

        latest_id = None

        for e in feed.entries:

            arxiv_id = e.id.split("/abs/")[-1]
            title = e.title.replace("\n", " ")

            # the papers are sorted now such that the first is the
            # most recent -- we want to store this id, so the next
            # time we run the script, we can pick up from here
            if latest_id is None:
                latest_id = arxiv_id

            # now check if we hit the old_id -- this is where we
            # left off last time.  Note things may not be in id order,
            # so we keep looking through the entire list of returned
            # results.
            if old_id is not None:
                # strip off any version number at the end, e.g. vN
                if float(arxiv_id.split("v")[0]) < float(old_id.split("v")[0]):
                    continue

            # link
            for l in e.links:
                if l.rel == "alternate":
                    url = l.href

            abstract = e.summary

            # any keyword matches?
            # we do two types of matches here.  If the keyword tuple has the "any"
            # qualifier, then we don't care how it appears in the text, but if
            # it has "unique", then we want to make sure only that word matches,
            # i.e., "nova" and not "supernova".  If any of the exclude words associated
            # with the keyword are present, then we reject any match

            def get_match(k):
                # returns bools for [matched?], [excluded?]
                # first check the "NOT"s
                excluded = False
                for n in k.excludes:
                    if n in abstract.lower().replace("\n", " ") or n in title.lower():
                        # we've matched one of the excludes
                        excluded = True

                if excluded:
                    return True, True

                if k.matching == "any":
                    if k.name in abstract.lower().replace("\n", " ") or k.name in title.lower():
                        return True, False

                elif k.matching == "unique":
                    qa = [l.lower().strip('\":.,!?') for l in abstract.split()]
                    qt = [l.lower().strip('\":.,!?') for l in title.split()]
                    if k.name in qa + qt:
                        return True, False

                return False, False

            meets_requirements = True
            key_match_results = []
            for k in keywords:
                match, excluded = get_match(k)

                if k.required and not match:
                    meets_requirements = False
                    break

                if match and not excluded:
                    key_match_results.append(k)

            keys_matched = []
            channels = []
            for k in key_match_results:
                keys_matched.append(k.name)
                channels.append(k.channel)

            # multiple channels can list the same keyword, so do not double count them
            keys_matched = list(set(keys_matched))
            if keys_matched:
                results.append(Paper(arxiv_id, title, url, keys_matched, channels))

        return results, latest_id


def report(body, subject, sender, receiver):
    """ send an email """

    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = receiver

    try:
        sm = smtplib.SMTP('localhost')
        sm.sendmail(sender, receiver, msg.as_string())
    except smtplib.SMTPException:
        sys.exit("ERROR sending mail")


def search_arxiv(keywords, old_id=None, categories=["astro-ph"]):
    """ do the actual search though each requested arxiv category by first querying
        the category for the latest papers and then looking for keyword matches"""

    if "all" in categories:
        categories = ArxivCategoryMap.keys()

    today = dt.date.today()
    day = dt.timedelta(days=1)

    max_papers = 1000

    cat_papers = []
    cat_last_id = "0000.00000"

    for category in categories:
        # we pick a wide-enough search range to ensure we catch papers
        # if there is a holiday

        # also, something wierd happens -- the arxiv ids appear to be
        # in descending order if you look at the "pastweek" listing
        # but the submission dates can vary wildly.  It seems that some
        # papers are held for a week or more before appearing.
        q = ArxivQuery(today - 10*day, today, max_papers, old_id=old_id, category=category)
        print(q.get_url())

        papers, last_id = q.do_query(keywords=keywords, old_id=old_id)

        cat_papers += papers
        if float(cat_last_id.split("v")[0]) < float(last_id.split("v")[0]):
            cat_last_id = last_id

    # a paper can be posted to multiple arxiv categories, so converting
    # to a set eliminates duplicates and then we sort the papers
    cat_papers = list(set(cat_papers))
    cat_papers.sort(reverse=True)

    return cat_papers, cat_last_id


def filter_keyword_requires(papers, channel_req=None):
    # filter out papers that do not match number of required keywords
    if channel_req is None:
        return papers
    else:
        filtered_papers = []
        for p in papers:
            p.posted_to_slack = False
        for c in channel_req:
            for p in papers:
                if not p.posted_to_slack:
                    if c in p.channels:
                        if len(p.keywords) >= channel_req[c]:
                            filtered_papers.append(p)
                            p.posted_to_slack = 1
        return filtered_papers


def send_email(papers, mail=None):

    # compose the body of our e-mail
    body = ""

    # sort papers by keywords
    current_kw = None
    for p in papers:
        if not p.kw_str() == current_kw:
            current_kw = p.kw_str()
            body += "\nkeywords: {}\n\n".format(current_kw)

        body += u"{}\n".format(p)

    # e-mail it
    if not len(papers) == 0:
        if not mail is None:
            report(body, "astro-ph papers of interest",
                   "lazy-astroph@{}".format(platform.node()), mail)
        else:
            print(body)


def run(string):
    """ run a UNIX command """

    # shlex.split will preserve inner quotes
    prog = shlex.split(string)
    p0 = subprocess.Popen(prog, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT)

    stdout0, stderr0 = p0.communicate()
    rc = p0.returncode
    p0.stdout.close()

    return stdout0, stderr0, rc


def slack_post(papers, channel_req, username=None, icon_emoji=None, webhook=None):
    """ post the information to a slack channel """

    # loop by channel
    for c in channel_req:
        channel_body = ""
        for p in papers:
            if not p.posted_to_slack:
                if c in p.channels:
                    if len(p.keywords) >= channel_req[c]:
                        keywds = ", ".join(p.keywords).strip()
                        channel_body += u"{} [{}]\n\n".format(p, keywds)
                        p.posted_to_slack = 1

        if webhook is None:
            print("channel: {}".format(c))
            continue

        payload = {}
        payload["channel"] = c
        if username is not None:
            payload["username"] = username
        if icon_emoji is not None:
            payload["icon_emoji"] = icon_emoji
        payload["text"] = channel_body

        cmd = "curl -X POST --data-urlencode 'payload={}' {}".format(json.dumps(payload), webhook)
        run(cmd)

def doit():
    """ the main driver for the lazy-astroph script """

    # parse runtime parameters
    parser = argparse.ArgumentParser()

    parser.add_argument("-m", help="e-mail address to send report to",
                        type=str, default=None)
    parser.add_argument("inputs", help="inputs file containing keywords",
                        type=str, nargs=1)
    parser.add_argument("-w", help="file containing slack webhook URL",
                        type=str, default=None)
    parser.add_argument("-u", help="slack username appearing in post",
                        type=str, default=None)
    parser.add_argument("-e", help="slack icon_emoji appearing in post",
                        type=str, default=None)
    parser.add_argument("-c", "--categories", nargs="+", type=str, default=["astro-ph"],
            help="list of arxiv categories to search (e.g. 'astro-ph.HE' -- use 'all' to search all arXiv categories. Default: search all astro-ph categories)")
    parser.add_argument("--dry_run",
                        help="don't send any mail or slack posts and don't update the marker where we left off",
                        action="store_true")
    parser.add_argument("-l", "--label", type=str, default=None,
                        help="label for this run of the script (to uniquely identify its param file)")
    args = parser.parse_args()

    # get the keywords
    keywords = []
    try:
        f = open(args.inputs[0], "r")
    except:
        sys.exit("ERROR: unable to open inputs file")
    else:
        channel = None
        channel_req = {}
        for line in f:
            l = line.lower().rstrip()

            if l == "":
                continue

            elif l.startswith("#") or l.startswith("@"):
                # this line defines a channel
                ch = l.split()
                channel = ch[0]
                if len(ch) == 2:
                    requires = int(ch[1].split("=")[1])
                else:
                    requires = 1
                channel_req[channel] = requires

            else:
                # this line has a keyword (and optional NOT keywords)
                if "not:" in l:
                    kw, nots = l.split("not:")
                    kw = kw.strip()
                    excludes = [x.strip() for x in nots.split(",")]
                else:
                    kw = l.strip()
                    excludes = []

                last_two = kw[-2:]
                if "-" in last_two:
                    matching = "unique"
                    kw = kw[:len(kw)-1]
                else:
                    matching = "any"

                required = False
                if "!" in last_two:
                    required = True
                    kw = kw[:len(kw)-1]

                keywords.append(Keyword(kw, matching=matching, required=required,
                                        channel=channel, excludes=excludes))

    # have we done this before? if so, read the .lazy_astroph file to get
    # the id of the paper we left off with
    param_file = os.path.expanduser("~") + "/.lazy_astroph"
    if args.label:
        param_file = param_file + f"_{args.label}"
    print(f"using param file: {param_file}")

    try:
        f = open(param_file, "r")
    except:
        old_id = None
    else:
        old_id = f.readline().rstrip()
        f.close()

    papers, last_id = search_arxiv(keywords, old_id=old_id, categories=args.categories)

    if not args.dry_run:
        papers = filter_keyword_requires(papers, channel_req)
        send_email(papers, mail=args.m)

        if not args.w is None:
            try:
                f = open(args.w)
            except:
                sys.exit("ERROR: unable to open webhook file")

            webhook = str(f.readline())
            f.close()
        else:
            webhook = None

        slack_post(papers, channel_req, icon_emoji=args.e, username=args.u, webhook=webhook)

        print("writing param_file", param_file)

        try:
            f = open(param_file, "w")
        except:
            sys.exit("ERROR: unable to open parameter file for writting")
        else:
            f.write(last_id)
            f.close()
    else:
        papers = filter_keyword_requires(papers, channel_req)
        send_email(papers, mail=None)

if __name__ == "__main__":
    doit()
