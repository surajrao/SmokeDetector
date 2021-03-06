# -*- coding: utf-8 -*-
# noinspection PyCompatibility

import regex
from difflib import SequenceMatcher
from urllib.parse import urlparse
from itertools import chain
from collections import Counter
from datetime import datetime

# noinspection PyPackageRequirements
import tld
# noinspection PyPackageRequirements
from tld.utils import TldDomainNotFound
import phonenumbers
import dns.resolver

from helpers import all_matches_unique, log
from globalvars import GlobalVars
from blacklists import load_blacklists

SIMILAR_THRESHOLD = 0.95
SIMILAR_ANSWER_THRESHOLD = 0.7
CHARACTER_USE_RATIO = 0.42
EXCEPTION_RE = r"^Domain (.*) didn't .*!$"
RE_COMPILE = regex.compile(EXCEPTION_RE)
COMMON_MALFORMED_PROTOCOLS = [
    ('httl://', 'http://'),
]
SE_SITES_RE = r'(?:{sites})'.format(
    sites='|'.join([
        r'([a-z]+\.)stackoverflow\.com',
        r'(?:{doms})\.com'.format(doms='|'.join(
            [r'askubuntu', r'superuser', r'serverfault'])),
        r'mathoverflow\.net',
        r'(?:[a-z]+\.)*stackexchange\.com']))


# Flee before the ugly URL validator regex!
# We are using this, instead of a nice library like BeautifulSoup, because spammers are
# stupid and don't always know how to actually *link* their web site. BeautifulSoup misses
# those plain text URLs.
# https://gist.github.com/dperini/729294#gistcomment-1296121
URL_REGEX = regex.compile(
    r"""((?:(?:https?|ftp)://)(?:\S+(?::\S*)?@)?(?:(?!(?:10|127)"""
    r"""(?:\.\d{1,3}){3})(?!(?:169\.254|192\.168)(?:\.\d{1,3}){2}))"""
    r"""(?!172\.(?:1[6-9]|2\d|3[0-1])(?:\.\d{1,3}){2})(?:[1-9]\d?|1\d\d|2[01]\d|22[0-3])"""
    r"""(?:\.(?:1?\d{1,2}|2[0-4]\d|25[0-5])){2}(?:\.(?:[1-9]\d?|1\d\d|2[0-4]\d|25[0-4]))"""
    r"""|(?:(?:[a-z\u00a1-\uffff0-9]-?)*[a-z\u00a1-\uffff0-9]+)(?:\.(?:[a-z\u00a1-\uffff0-9]-?)"""
    r"""*[a-z\u00a1-\uffff0-9]+)*(?:\.(?:[a-z\u00a1-\uffff]{2,})))(?::\d{2,5})?(?:/\S*)?""", regex.UNICODE)


# noinspection PyUnusedLocal,PyMissingTypeHints,PyTypeChecker
def has_repeated_words(s, site, *args):
    words = regex.split(r"[\s.,;!/\()\[\]+_-]", s)
    words = [word for word in words if word != ""]
    streak = 0
    prev = ""
    for word in words:
        if word == prev and word.isalpha() and len(word) > 1:
            streak += 1
        else:
            streak = 0
        prev = word
        if streak >= 5 and streak * len(word) >= 0.2 * len(s):
            return True, u"Repeated word: *{}*".format(word)
    return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints
def has_few_characters(s, site, *args):
    s = regex.sub("</?p>", "", s).rstrip()    # remove HTML paragraph tags from posts
    uniques = len(set(list(s)))
    if (len(s) >= 30 and uniques <= 6) or (len(s) >= 100 and uniques <= 15):    # reduce if false reports appear
        if (uniques <= 15) and (uniques >= 5) and site == "math.stackexchange.com":
            # Special case for Math.SE: Uniques case may trigger false-positives.
            return False, ""
        return True, u"Contains {} unique characters".format(uniques)
    return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints
def has_repeating_characters(s, site, *args):
    s = regex.sub('http[^"]*', "", s)    # remove URLs for this check
    if s is None or len(s) == 0 or len(s) >= 300 or regex.compile("<pre>|<code>").search(s):
        return False, ""
    matches = regex.compile(u"([^\\s_\u200b\u200c.,?!=~*/0-9-])(\\1{10,})", regex.UNICODE).findall(s)
    match = "".join(["".join(match) for match in matches])
    if (100 * len(match) / len(s)) >= 20:  # Repeating characters make up >= 20 percent
        return True, u"Repeated character: *{}*".format(match)
    return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints
def link_at_end(s, site, *args):   # link at end of question, on selected sites
    s = regex.sub("</strong>|</em>|</p>", "", s)
    match = regex.compile(r"(?i)https?://(?:[.A-Za-z0-9-]*/?[.A-Za-z0-9-]*/?|plus\.google\.com/"
                          r"[\w/]*|www\.pinterest\.com/pin/[\d/]*)</a>\s*$").search(s)
    if match and not regex.compile(r"(?i)upload|\b(imgur|yfrog|gfycat|tinypic|sendvid|ctrlv|prntscr|gyazo|youtu\.?be|"
                                   r"stackexchange|superuser|past[ie].*|dropbox|microsoft|newegg|cnet|regex101|"
                                   r"(?<!plus\.)google|localhost|ubuntu|getbootstrap|"
                                   r"jsfiddle\.net|codepen\.io)\b").search(match.group(0)):
        return True, u"Link at end: {}".format(match.group(0))
    return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints,PyTypeChecker
def non_english_link(s, site, *args):   # non-english link in short answer
    if len(s) < 600:
        links = regex.compile(r'nofollow(?: noreferrer)?">([^<]*)(?=</a>)', regex.UNICODE).findall(s)
        for link_text in links:
            word_chars = regex.sub(r"(?u)\W", "", link_text)
            non_latin_chars = regex.sub(r"\w", "", word_chars)
            if len(word_chars) >= 1 and ((len(word_chars) <= 20 and len(non_latin_chars) >= 1) or
                                         (len(non_latin_chars) >= 0.05 * len(word_chars))):
                return True, u"Non-English link text: *{}*".format(link_text)
    return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints,PyTypeChecker
def mostly_non_latin(s, site, *args):   # majority of post is in non-Latin, non-Cyrillic characters
    word_chars = regex.sub(r'(?u)[\W0-9]|http\S*', "", s)
    non_latin_chars = regex.sub(r"(?u)\p{script=Latin}|\p{script=Cyrillic}", "", word_chars)
    if len(non_latin_chars) > 0.4 * len(word_chars):
        return True, u"Text contains {} non-Latin characters out of {}".format(len(non_latin_chars), len(word_chars))
    return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints
def has_phone_number(s, site, *args):
    if regex.compile(r"(?i)\b(address(es)?|run[- ]?time|error|value|server|hostname|timestamp|warning|code|"
                     r"(sp)?exception|version|chrome|1234567)\b", regex.UNICODE).search(s):
        return False, ""  # not a phone number
    s = regex.sub("[^A-Za-z0-9\\s\"',]", "", s)   # deobfuscate
    s = regex.sub("[Oo]", "0", s)
    s = regex.sub("[Ss]", "5", s)
    s = regex.sub("[Ii]", "1", s)
    matched = regex.compile(r"(?<!\d)(?:\d{2}\s?\d{8,11}|\d\s{0,2}\d{3}\s{0,2}\d{3}\s{0,2}\d{4}|8\d{2}"
                            r"\s{0,2}\d{3}\s{0,2}\d{4})(?!\d)", regex.UNICODE).findall(s)
    test_formats = ["IN", "US", "NG", None]      # ^ don't match parts of too long strings of digits
    for phone_number in matched:
        if regex.compile(r"^21474(672[56]|8364)|^192168").search(phone_number):
            return False, ""  # error code or limit of int size, or 192.168 IP
        for testf in test_formats:
            try:
                z = phonenumbers.parse(phone_number, testf)
                if phonenumbers.is_possible_number(z) and phonenumbers.is_valid_number(z):
                    log('debug', "Possible {}, Valid {}, Explain: {}".format(phonenumbers.is_possible_number(z),
                                                                             phonenumbers.is_valid_number(z), z))
                    return True, u"Phone number: {}".format(phone_number)
            except phonenumbers.phonenumberutil.NumberParseException:
                pass
    return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints
def has_customer_service(s, site, *args):  # flexible detection of customer service in titles
    s = s[0:300].lower()   # if applied to body, the beginning should be enough: otherwise many false positives
    s = regex.sub(r"[^A-Za-z0-9\s]", "", s)   # deobfuscate
    phrase = regex.compile(r"(tech(nical)? support)|((support|service|contact|help(line)?) (telephone|phone|"
                           r"number))").search(s)
    if phrase and site in ["askubuntu.com", "webapps.stackexchange.com", "webmasters.stackexchange.com"]:
        return True, u"Key phrase: *{}*".format(phrase.group(0))
    business = regex.compile(
        r"(?i)\b(airlines?|apple|AVG|BT|netflix|dell|Delta|epson|facebook|gmail|google|hotmail|hp|"
        r"lexmark|mcafee|microsoft|norton|out[l1]ook|quickbooks|sage|windows?|yahoo)\b").search(s)
    digits = len(regex.compile(r"\d").findall(s))
    if business and digits >= 5:
        keywords = regex.compile(r"(?i)\b(customer|help|care|helpline|reservation|phone|recovery|service|support|"
                                 r"contact|tech|technical|telephone|number)\b").findall(s)
        if len(set(keywords)) >= 2:
            matches = ", ".join(["".join(match) for match in keywords])
            return True, u"Scam aimed at *{}* customers. Keywords: *{}*".format(business.group(0), matches)
    return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints
def has_health(s, site, *args):   # flexible detection of health spam in titles
    s = s[0:200]   # if applied to body, the beginning should be enough: otherwise many false positives
    capitalized = len(regex.compile(r"\b[A-Z][a-z]").findall(s)) >= 5   # words beginning with uppercase letter
    organ = regex.compile(r"(?i)\b(colon|skin|muscle|bicep|fac(e|ial)|eye|brain|IQ|mind|head|hair|peni(s|le)|"
                          r"breast|body|joint|belly|digest\w*)s?\b").search(s)
    condition = regex.compile(r"(?i)\b(weight|constipat(ed|ion)|dysfunction|swollen|sensitive|wrinkle|aging|"
                              r"suffer|acne|pimple|dry|clog(ged)?|inflam(ed|mation)|fat|age|pound)s?\b").search(s)
    goal = regex.compile(r"(?i)\b(supple|build|los[es]|power|burn|erection|tone(d)|rip(ped)?|bulk|get rid|mood)s?\b|"
                         r"\b(diminish|look|reduc|beaut|renew|young|youth|lift|eliminat|enhance|energ|shred|"
                         r"health(?!kit)|improve|enlarge|remov|vital|slim|lean|boost|str[oe]ng)").search(s)
    remedy = regex.compile(r"(?i)\b(remed(y|ie)|serum|cleans?(e|er|ing)|care|(pro)?biotic|herbal|lotion|cream|"
                           r"gel|cure|drug|formula|recipe|regimen|solution|therapy|hydration|soap|treatment|supplement|"
                           r"diet|moist\w*|injection|potion|ingredient|aid|exercise|eat(ing)?)s?\b").search(s)
    boast = regex.compile(r"(?i)\b(most|best|simple|top|pro|real|mirac(le|ulous)|secrets?|organic|natural|perfect|"
                          r"ideal|fantastic|incredible|ultimate|important|reliable|critical|amazing|fast|good)\b|"
                          r"\b(super|hyper|advantag|benefi|effect|great|valu|eas[iy])").search(s)
    other = regex.compile(r"(?i)\b(product|thing|item|review|advi[cs]e|myth|make use|your?|really|work|tip|shop|"
                          r"store|method|expert|instant|buy|fact|consum(e|ption)|baby|male|female|men|women|grow|"
                          r"idea|suggest\w*|issue)s?\b").search(s)
    score = 4 * bool(organ) + 2 * bool(condition) + 2 * bool(goal) + 2 * bool(remedy) + bool(boast) + \
        bool(other) + capitalized
    if score >= 8:
        match_objects = [organ, condition, goal, remedy, boast, other]
        words = []
        for match in match_objects:
            if match:
                words.append(match.group(0))
        return True, u"Health-themed spam (score {}). Keywords: *{}*".format(score, ", ".join(words).lower())
    return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints
def pattern_product_name(s, site, *args):
    keywords = ["Testo?", "Dermapholia", "Garcinia", "Cambogia", "Aurora", "Kamasutra", "HL-?12", "NeuroFuse",
                "Junivive", "Apexatropin", "Gain", "Allure", "Nuvella", "Trimgenix", "Satin", "Prodroxatone",
                "Elite", "Force", "Exceptional", "Enhance(ment)?", "Nitro", "Max", "Boost", "E?xtreme", "Grow",
                "Deep", "Male", "Pro", "Advanced", "Monster", "Divine", "Royale", "Angele", "Trinity", "Andro",
                "Pure", "Skin", "Sea", "Muscle", "Ascend", "Youth", "Hyper(tone)?", "Hydroluxe", "Booster",
                "Serum", "Supplement", "Fuel", "Cream"]
    if site != "math.stackexchange.com" and site != "mathoverflow.net":
        keywords += ["E?X[tl\\d]?", "Alpha", "Plus", "Prime", "Formula"]
    keywords = "|".join(keywords)
    three_words = regex.compile(r"(?i)\b(({0})[ -]({0})[ -]({0}))\b".format(keywords)).findall(s)
    two_words = regex.compile(r"(?i)\b(({0})[ -]({0}))\b".format(keywords)).findall(s)
    if len(three_words) >= 1 and all_matches_unique(three_words):
        return True, u"Pattern-matching product name *{}*".format(three_words[0][0])
    elif len(two_words) >= 2 and all_matches_unique(two_words):
        return True, u"Pattern-matching product name *{}*".format(two_words[0][0])
    return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints
def what_is_this_pharma_title(s, site, *args):   # title "what is this Xxxx?"
    if regex.compile(r'^what is this (?:[A-Z]|http://)').match(s):
        return True, u'Title starts with "what is this"'
    else:
        return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints
def keyword_email(s, site, *args):   # a keyword and an email in the same post
    if regex.compile("<pre>|<code>").search(s) and site == "stackoverflow.com":  # Avoid false positives on SO
        return False, ""
    keyword = regex.compile(r"(?i)\b(training|we (will )?(offer|develop|provide)|sell|invest(or|ing|ment)|credit|"
                            r"money|quality|legit|interest(ed)?|guarantee|rent|crack|opportunity|fundraising|campaign|"
                            r"career|employment|candidate|loan|lover|husband|wife|marriage|illuminati|brotherhood|"
                            r"(join|contact) (me|us|him)|reach (us|him)|spell(caster)?|doctor|cancer|krebs|"
                            r"(cheat|hack)(er|ing)?|spying|passport|seaman|scam|pics|vampire|bless(ed)?|atm|miracle|"
                            r"cure|testimony|kidney|hospital|wetting)s?\b| Dr\.? |\$ ?[0-9,.]{4}|@qq\.com|"
                            r"\b(герпес|муж|жена|доктор|болезн)").search(s)
    email = regex.compile(r"(?<![=#/])\b[A-z0-9_.%+-]+@(?!(example|domain|site|foo|\dx)\.[A-z]{2,4})"
                          r"[A-z0-9_.%+-]+\.[A-z]{2,4}\b").search(s)
    if keyword and email:
        return True, u"Keyword *{}* with email {}".format(keyword.group(0), email.group(0))
    obfuscated_email = regex.compile(r"(?<![=#/])\b[A-z0-9_.%+-]+ *@ *(g *mail|yahoo) *\. *com\b").search(s)
    if obfuscated_email and not email:
        return True, u"Obfuscated email {}".format(obfuscated_email.group(0))
    return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints
def pattern_email(s, site, *args):
    pattern = regex.compile(r"(?<![=#/])\b(dr|[A-z0-9_.%+-]*"
                            r"(loan|hack|financ|fund|spell|temple|herbal|spiritual|atm|heal|priest|classes|"
                            r"investment))[A-z0-9_.%+-]*"
                            r"@(?!(example|domain|site|foo|\dx)\.[A-z]{2,4})[A-z0-9_.%+-]+\.[A-z]{2,4}\b"
                            ).search(s.lower())
    if pattern:
        return True, u"Pattern-matching email {}".format(pattern.group(0))
    return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints
def keyword_link(s, site, *args):   # thanking keyword and a link in the same short answer
    if len(s) > 400:
        return False, ""
    link = regex.compile(r'(?i)<a href="https?://\S+').search(s)
    if not link or regex.compile(r"(?i)upload|\b(imgur|yfrog|gfycat|tinypic|sendvid|ctrlv|prntscr|gyazo|youtu\.?be|"
                                 r"stackexchange|superuser|past[ie].*|dropbox|microsoft|newegg|cnet|(?<!plus\.)google|"
                                 r"localhost|ubuntu)\b").search(link.group(0)):
        return False, ""
    praise = regex.compile(r"(?i)\b(nice|good|interesting|helpful|great|amazing) (article|blog|post|information)\b|"
                           r"very useful").search(s)
    thanks = regex.compile(r"(?i)\b(appreciate|than(k|ks|x))\b").search(s)
    keyword = regex.compile(r"(?i)\b(I really appreciate|many thanks|thanks a lot|thank you (very|for)|"
                            r"than(ks|x) for (sharing|this|your)|dear forum members|(very (informative|useful)|"
                            r"stumbled upon (your|this)|wonderful|visit my) (blog|site|website))\b").search(s)
    if link and keyword:
        return True, u"Keyword *{}* with link {}".format(keyword.group(0), link.group(0))
    if link and thanks and praise:
        return True, u"Keywords *{}*, *{}* with link {}".format(thanks.group(0), praise.group(0), link.group(0))
    return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints
def bad_link_text(s, site, *args):   # suspicious text of a hyperlink
    s = regex.sub("</?strong>|</?em>", "", s)  # remove font tags
    keywords = regex.compile(
        r"(?isu)"
        r"\b(buy|cheap) |live[ -]?stream|"
        r"\bmake (money|\$)|"
        r"\b(porno?|(whole)?sale|coins|replica|luxury|essays?|in \L<city>)\b"
        r"\b\L<city>(?:\b.{1.20}\b)?(service|escort|call girls?)|"
        r"(best|make|full|hd|software|cell|data)[\w ]{1,20}(online|service|company|repair|recovery)|"
        r"\b(writing service|essay (writing|tips))", city=FindSpam.city_list)
    links = regex.compile(r'nofollow(?: noreferrer)?">([^<]*)(?=</a>)', regex.UNICODE).findall(s)
    business = regex.compile(
        r"(?i)(^| )(airlines?|apple|AVG|BT|netflix|dell|Delta|epson|facebook|gmail|google|hotmail|hp|"
        r"lexmark|mcafee|microsoft|norton|out[l1]ook|quickbooks|sage|windows?|yahoo)($| )")
    support = regex.compile(r"(?i)(^| )(customer|care|helpline|reservation|phone|recovery|service|support|contact|"
                            r"tech|technical|telephone|number)($| )")
    for link_text in links:
        keywords_match = keywords.search(link_text)
        if keywords_match:
            return True, u"Bad keyword *{}* in link text".format(keywords_match.group(0).strip())
        business_match = business.search(link_text)
        support_match = support.search(link_text)
        if business_match and support_match:
            return True, u"Bad keywords *{}*, *{}* in link text".format(business_match.group(0).strip(),
                                                                        support_match.group(0).strip())
    return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints
def bad_pattern_in_url(s, site, *args):
    patterns = [
        r'[^"]*-reviews?(?:-(?:canada|(?:and|or)-scam))?/?',
        r'[^"]*-support/?',
    ]
    matches = regex.compile(
        r'<a href="(?P<frag>{0})"|<a href="[^"]*"(?:\s+"[^"]*")*>(?P<frag>{0})</a>'.format(
            '|'.join(patterns)), regex.UNICODE).findall(s)
    matches = [x for x in matches if not regex.match(
        r'^https?://{0}'.format(SE_SITES_RE), x[0])]
    if matches:
        return True, u"Bad fragment in link {}".format(
            ", ".join(["".join(match) for match in matches]))
    else:
        return False, ""


def bad_ns_for_url_domain(s, site, *args):
    for domain in set([get_domain(link, full=True) for link in post_links(s)]):
        if not tld.get_tld(domain, fix_protocol=True, fail_silently=True):
            log('debug', '{0} has no valid tld; skipping'.format(domain))
            continue
        try:
            starttime = datetime.now()
            ns = dns.resolver.query(domain, 'ns')
        except dns.exception.DNSException as exc:
            if str(exc).startswith('None of DNS query names exist:'):
                log('debug', 'domain {0} not found; skipping'.format(domain))
                continue
            endtime = datetime.now()
            log('warning', 'DNS error {0} (duration: {1})'.format(
                exc, endtime - starttime))
            continue
        endtime = datetime.now()
        log('info', 'NS query duration {0}'.format(endtime - starttime))
        nameservers = [server.target.to_text() for server in ns]
        if any([ns.endswith('.namecheaphosting.com.') for ns in nameservers]):
            return True, '{domain} NS suspicious {ns}'.format(
                domain=domain, ns=','.join(nameservers))
    return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints
def is_offensive_post(s, site, *args):
    if s is None or len(s) == 0:
        return False, ""

    offensive = regex.compile(r"(?is)\b(ur mom|(yo)?u suck|8={3,}D|nigg[aeu][rh]?|(ass ?|a|a-)hole|fag(got)?|"
                              r"daf[au][qk]|(?<!brain)(mother|mutha)?fuc?k+(a|ing?|e?(r|d)| off+| y(ou|e)(rself)?|"
                              r" u+|tard)?|shit(t?er|head)|you scum|dickhead|pedo|whore|cunt|cocksucker|ejaculated?|"
                              r"jerk off|cummies|butthurt|queef|(private|pussy) show|lesbo|"
                              r"bitche?s?|(eat|suck)\b.{0,20}\b dick|dee[sz]e? nut[sz])s?\b")
    matches = offensive.finditer(s)
    len_of_match = 0
    text_matched = []
    for match in matches:
        len_of_match += match.end() - match.start()
        text_matched.append(match.group(0))

    if (1000 * len_of_match / len(s)) >= 15:  # currently at 1.5%, this can change if it needs to
        return True, u"Offensive keyword{}: *{}*".format("s" if len(text_matched) > 1 else "", ", ".join(text_matched))
    return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints
def has_eltima(s, site, *args):
    reg = regex.compile(r"(?is)\beltima")
    if reg.search(s) and len(s) <= 750:
        return True, u"Bad keyword *eltima* and body length under 750 chars"
    return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints,PyTypeChecker
def username_similar_website(s, site, *args):
    username = args[0]
    sim_result = perform_similarity_checks(s, username)
    if sim_result >= SIMILAR_THRESHOLD:
        return True, u"Username similar to website"
    else:
        return False, ""


# noinspection PyUnusedLocal,PyMissingTypeHints,PyTypeChecker
def character_utilization_ratio(s, site, *args):
    counter = Counter(s)
    total_chars = len(s)
    highest_ratio = 0.0
    # highest_char = None

    for key, value in counter.items():
        char_ratio = value / float(total_chars)
        key, value, char_ratio
        if char_ratio > highest_ratio:
            highest_ratio = char_ratio
            # highest_char = key

    if highest_ratio > CHARACTER_USE_RATIO:
        return True, "The `{}` character appears in a high percentage of the post"
    else:
        return False, ""


def post_links(post):
    """
    Helper function to extract URLs from a piece of HTML.
    """
    # Fix stupid spammer tricks
    for p in COMMON_MALFORMED_PROTOCOLS:
        post = post.replace(p[0], p[1])

    links = []
    for l in regex.findall(URL_REGEX, post):
        if l[-1].isalnum():
            links.append(l)
        else:
            links.append(l[:-1])

    return set(links)


# noinspection PyMissingTypeHints
def perform_similarity_checks(post, name):
    """
    Performs 4 tests to determine similarity between links in the post and the user name
    :param post: Test of the post
    :param name: Username to compare against
    :return: Float ratio of similarity
    """
    t1 = 0
    t2 = 0
    t3 = 0
    t4 = 0

    for link in post_links(post):
        domain = get_domain(link)
        # Straight comparison
        t1 = similar_ratio(domain, name)
        # Strip all spaces check
        t2 = similar_ratio(domain, name.replace(" ", ""))
        # Strip all hypens
        t3 = similar_ratio(domain.replace("-", ""), name.replace("-", ""))
        # Strip both hypens and spaces
        t4 = similar_ratio(domain.replace("-", "").replace(" ", ""), name.replace("-", "").replace(" ", ""))
        # Have we already exceeded the threshold? End now if so, otherwise, check the next link
        if max(t1, t2, t3, t4) >= SIMILAR_THRESHOLD:
            break

    return max(t1, t2, t3, t4)


# noinspection PyMissingTypeHints
def similar_ratio(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


# noinspection PyMissingTypeHints
def get_domain(s, full=False):
    """
    Extract the domain name; with full=True, keep the TLD tacked on.
    """
    try:
        extract = tld.get_tld(s, fix_protocol=True, as_object=True, )
        if full:
            domain = str(extract)
        else:
            domain = extract.domain
    except TldDomainNotFound as e:
        invalid_tld = RE_COMPILE.match(str(e)).group(1)
        # Attempt to replace the invalid protocol
        s1 = s.replace(invalid_tld, 'http', 1)
        try:
            extract = tld.get_tld(s1, fix_protocol=True, as_object=True, )
            if full:
                domain = str(extract)
            else:
                domain = extract.domain
        except TldDomainNotFound:
            # Assume bad TLD and try one last fall back, just strip the trailing TLD and leading subdomain
            parsed_uri = urlparse(s)
            if len(parsed_uri.path.split(".")) >= 3:
                if full:
                    domain = '.'.join(parsed_uri.path.split(".")[1:])
                else:
                    domain = parsed_uri.path.split(".")[1]
            else:
                if full:
                    domain = parsed_uri.path
                else:
                    domain = parsed_uri.path.split(".")[0]
    return domain


# noinspection PyMissingTypeHints
def similar_answer(post):
    if not post.parent:
        return False, False, False, ""

    question = post.parent
    sanitized_body = strip_urls_and_tags(post.body)

    for other_answer in question.answers:
        if other_answer.post_id != post.post_id:
            sanitized_answer = strip_urls_and_tags(other_answer.body)
            ratio = similar_ratio(sanitized_body, sanitized_answer)

            if ratio >= SIMILAR_ANSWER_THRESHOLD:
                return False, False, True, \
                    u"Answer similar to answer {}, ratio {}".format(other_answer.post_id, ratio)

    return False, False, False, ""


# noinspection PyMissingTypeHints
def strip_urls_and_tags(string):
    return regex.sub(r"</?.+?>|\w+?://", "", regex.sub(URL_REGEX, "", string))


# noinspection PyUnusedLocal,PyMissingTypeHints
def mostly_dots(s, site, *args):
    body = strip_urls_and_tags(s)
    body_length = len(body)

    dot_count = len(regex.findall(r"\.", body))

    if body_length and dot_count / float(body_length) >= 0.4:
        return True, u"Post contains {} dots out of {} characters".format(dot_count, body_length)
    else:
        return False, ""


def mevaqesh_troll(s, *args):
    s = s.lower().replace(' ', '')
    bad = 'mevaqeshthereforehasnoshareintheworldtocome'
    if bad in s:
        return True, "Post matches pattern from a known troll"
    else:
        return False, ""


load_blacklists()


# noinspection PyClassHasNoInit
class FindSpam:
    bad_keywords_nwb = [  # "nwb" == "no word boundary"
        u"ಌ", "vashi?k[ae]r[ae]n", "babyli(ss|cious)", "garcinia", "cambogia", "acai ?berr",
        "(eye|skin|aging) ?cream", "b ?a ?m ?((w ?o ?w)|(w ?a ?r))", "online ?it ?guru",
        "abam26", "watch2live", "cogniq", "(serum|lift) ?eye", "tophealth", "poker[ -]?online",
        "caralluma", r"male\Wperf(?!ormer)", "anti[- ]?aging", "lumisse", "(ultra|berry|body)[ -]?ketone",
        "(cogni|oro)[ -]?(lift|plex)", "diabazole", "forskolin", "tonaderm", "luma(genex|lift)",
        "(skin|face|eye)[- ]?(serum|therapy|hydration|tip|renewal|gel|lotion|cream)",
        "(skin|eye)[- ]?lift", "(skin|herbal) ?care", "nuando[ -]?instant", "\\bnutra", "nitro[ -]?slim",
        "aimee[ -]?cream", "slimatrex", "cosmitone", "smile[ -]?pro[ -]?direct", "bellavei", "opuderm",
        r"contact (me|us)\W*<a ", "follicure", "kidney[ -]?bean[ -]?extract", "ecoflex",
        r"\brsgold", "bellavei", "goji ?xtreme", "lumagenex", "ajkobeshoes", "kreatine",
        "packers.{0,15}(movers|logistic).{0,25}</a>", "guaranteedprofitinvestment",
        "(brain|breast|male|penile|penis)[- ]?(enhance|enlarge|improve|boost|plus|peak)",
        "renuva(cell|derm)", " %uh ", " %ah ", "svelme", "tapsi ?sarkar", "viktminskning",
        "unique(doc)?producers", "green ?tone ?pro", "troxyphen", "seremolyn", "revolyn",
        "(?:networking|cisco|sas|hadoop|mapreduce|oracle|dba|php|sql|javascript|js|java|designing|marketing|"
        "salesforce|joomla)( certification)? (courses?|training).{0,25}</a>",
        r"(?:design|development|compan(y|ies)|training|courses?|automation)(\b.{1,8}\b)?\L<city>\b",
        u"Ｃ[Ｏ|0]Ｍ", "ecoflex", "no2factor", "no2blast", "sunergetic", "capilux", "sante ?avis",
        "enduros", "dianabol", r"ICQ#?\d{4}-?\d{5}", "3073598075", "lumieres", "viarex", "revimax",
        "celluria", "viatropin", "(meg|test)adrox", "nordic ?loan ?firm", r"safflower\Woil",
        "(essay|resume|article|dissertation|thesis) ?writing ?service", "satta ?matka", "b.?o.?j.?i.?t.?e.?r",
        r"rams[ey]+\W?dave"
    ]

    # Patterns: the top four lines are the most straightforward, matching any site with this string in domain name
    pattern_websites = [
        r"(enstella|recoverysoftware|removevirus|support(number|help|quickbooks)|techhelp|calltech|exclusive|"
        r"onlineshop|video(course|classes|tutorial(?!s))|vipmodel|(?<!word)porn|wholesale|inboxmachine|(get|buy)cheap|"
        r"escort|diploma|(govt|government)jobs|extramoney|earnathome|spell(caster|specialist)|profits|"
        r"seo-?(tool|service|trick|market)|onsale|fat(burn|loss)|(\.|//|best)cheap|online-?(training|solution))"
        r"[\w-]*?\.(co|net|org|in(\W|fo)|us|ir|wordpress|blogspot|tumblr|webs\.)",
        r"(replica(?!t)|rs\d?gold|rssong|runescapegold|maxgain|e-cash|mothers?day|phone-?number|fullmovie|tvstream|"
        r"trainingin|dissertation|(placement|research)-?(paper|statement|essay)|digitalmarketing|infocampus|"
        r"cracked\w{3}|bestmover|relocation|\w{4}mortgage|loans|revenue|testo[-bsx]|cleanse|cleansing|detox|supplement|"
        r"lubricant|serum|wrinkle|topcare|freetrial)[\w-]*?\.(co|net|org|in(\W|fo)|us|"
        r"wordpress|blogspot|tumblr|webs\.)",
        r"(drivingschool|crack-?serial|serial-?(key|crack)|freecrack|appsfor(pc|mac)|probiotic|remedies|heathcare|"
        r"sideeffect|meatspin|packers\S{0,3}movers|(buy|sell)\S{0,12}cvv|goatse|burnfat|gronkaffe|muskel|"
        r"tes(tos)?terone|nitric(storm|oxide)|masculin|menhealth|intohealth|babaji|spellcaster|potentbody|slimbody|"
        r"moist|lefair|derma(?![nt])|xtrm|factorx|(?<!app)nitro(?!us)|endorev|ketone)[\w-]*?\.(co|net|org|in(\W|fo)|us|"
        r"wordpress|blogspot|tumblr|webs\.)",
        r"(moving|\w{10}spell|[\w-]{3}password|(?!greatfurniture)\w{5}deal|(?!nfood)\w{5}facts|\w\dfacts|\Btoyshop|"
        r"[\w-]{5}cheats|"
        r"(?!djangogirls\.org(?:$|[/?]))[\w-]{6}girls|"
        r"clothing|shoes(inc)?|cheatcode|cracks|credits|-wallet|refunds|truo?ng|viet|"
        r"trang)\.(co|net|org|in(\W|fo)|us)",
        r"(health|earn|max|cash|wage|pay|pocket|cent|today)[\w-]{0,6}\d+\.com",
        r"(//|www\.)healthy?\w{5,}\.com",
        r"https?://[\w-.]\.repair\W", r"https?://[\w-.]{10,}\.(top|help)\W",
        r"filefix(er)?\.com", r"\.page\.tl\W", r"infotech\.(com|net|in)",
        r"\.(com|net)/(xtra|muscle)[\w-]", r"http\S*?\Wfor-sale\W",
        r"fifa\d+[\w-]*?\.com", r"[\w-](giveaway|jackets|supplys|male)\.com",
        r"((essay|resume|click2)\w{6,}|(essays|(research|term)paper|examcollection|[\w-]{5}writing|"
        r"writing[\w-]{5})[\w-]*?)\.(co|net|org|in(\W|fo)|us|us)",
        r"(top|best|expert)\d\w{0,15}\.in\W", r"\dth(\.co)?\.in", r"(jobs|in)\L<city>\.in",
        r"[\w-](recovery|repairs?|rescuer|(?<!epoch|font)converter)(pro|kit)?\.(com|net)",
        r"(corrupt|repair)[\w-]*?\.blogspot", r"http\S*?(yahoo|gmail|hotmail|outlook|office|microsoft)[\w-]{0,10}"
                                              r"(account|tech|customer|support|service|phone|help)[\w-]{0,10}(service|"
                                              r"care|help|recovery|support|phone|number)",
        r"http\S*?(essay|resume|thesis|dissertation|paper)-?writing",
        r"fix[\w-]*?(files?|tool(box)?)\.com", r"(repair|recovery|fix)tool(box)?\.(co|net|org)",
        r"smart(pc)?fixer\.(co|net|org)",
        r"password[\w-]*?(cracker|unlocker|reset|buster|master|remover)\.(co|net)",
        r"crack[\w-]*?(serial|soft|password)[\w-]*?\.(co|net)",
        r"(downloader|pdf)converter\.(com|net)",
        r"ware[\w-]*?download\.(com|net|info|in\W)",
        r"((\d|\w{3})livestream|livestream(ing|s))[\w]*?\.(com|net|tv)", r"\w+vs\w+live\.(com|net|tv)",
        r"(play|watch|cup|20)[\w-]*?(live|online)\.(com|net|tv)", r"worldcup\d[\w-]*?\.(com|net|tv|blogspot)",
        r"https?://(\w{5,}tutoring\w*|cheat[\w-.]{3,}|xtreme[\w-]{5,})\.",
        r"(platinum|paying|acai|buy|premium|premier|ultra|thebest|best|[/.]try)[\w]{10,}\.(co|net|org|in(\W|fo)|us)",
        r"(training|institute|marketing)[\w-]{6,}[\w.-]*?\.(co|net|org|in(\W|fo)|us)",
        r"[\w-](courses?|training)[\w-]*?\.in/", r"\w{9}(buy|roofing)\.(co|net|org|in(\W|fo)|us)",
        r"(vitamin|dive|hike|love|strong|ideal|natural|pro|magic|beware|top|best|free|cheap|allied|nutrition|"
        r"prostate)[\w-]*?health[\w-]*?\.(co|net|org|in(\W|fo)|us|wordpress|blogspot|tumblr|webs\.)",
        r"(eye|skin|age|aging)[\w-]*?cream[\w-]*?\.(co|net|org|in(\W|fo)|us|wordpress|blogspot|tumblr|webs\.)",
        r"(acai|advance|aging|alpha|beauty|belle|beta|biotic|body|boost(?! solution)|brain(?!tree)|burn|colon|"
        r"[^s]cream|creme|derma|ecig|eye|face(?!book)|fat|formula|geniu[sx]|grow|hair|health|herbal|ideal|luminous|"
        r"male|medical|medicare|muscle|natura|no2|nutrition|optimal|pearl|perfect|phyto|probio|rejuven|revive|ripped|"
        r"rx|scam|shred|skin|slim|super|testo|[/.]top|trim|[/.]try|ultra|ultra|vapor|vita|weight|wellness|xplode|yoga|"
        r"young|youth)[\w]{0,20}(about|advi[sc]|assess|blog|brazil|canada|care|center|centre|chat|complex(?!ity)|"
        r"congress|consult|critic|critique|cure|denmark|discussion|doctor|dose|essence|essential|extract|fact|formula|"
        r"france|funct?ion|genix|guide|help|idea|info|jacked|l[iy]ft|mag|market|max|mexico|norway|nutrition|order|plus|"
        r"points|policy|potency|power|practice|pro|program|report|review|rewind|site|slim|solution|suppl(y|ier)|sweden|"
        r"tip|trial|try|world|zone)[.\w-]{0,12}\.(co|net|org|in(\W|fo)|us|wordpress|blogspot|tumblr|webs\.)",
        r"(\w{11}(idea|income|sale)|\w{6}(<?!notebook)(advice|problog|review))s?\.(co|net|in(\W|fo)|us)",
        r"-(poker|jobs)\.com", r"send[\w-]*?india\.(co|net|org|in(\W|fo)|us)",
        r"(file|photo|android|iphone)recovery[\w-]*?\.(co|net|org|in(\W|fo)|us)",
        r"(videos?|movies?|watch)online[\w-]*?\.", r"hd(video|movie)[\w-]*?\.",
        r"backlink(?!(o\.|watch))[\w-]*?\.(co|net|org|in(\W|fo)|us)",
        r"(replica[^nt]\w{5,}|\wrolex)\.(co|net|org|in(\W|fo)|us)",
        r"customer(service|support)[\w-]*?\.(co|net|org|in(\W|fo)|us)",
        r"conferences?alert[\w-]*?\.(co|net|org|in(\W|fo)|us)",
        r"seo\.com(?!/\w)", r"\Wseo[\w-]{10,}\.(com|net|in\W)",
        r"(?<!site)24x7[\w-]*?\.(co|net|org|in(\W|fo)|us)",
        r"backlink[\w-]*?\.(com|net|de|blogspot)",
        r"(software|developers|packers|movers|logistic|service)[\w-]*?india\.(com|in\W)",
        r"scam[\w-]*?(book|alert|register|punch)[\w-]*?\.(co|net|org|in(\W|fo)|us)",
        r"http\S*?crazy(mass|bulk)", r'http\S*\.com\.com[/"<]',
        r"https?://[^/\s]{8,}healer",
        r'reddit\.com/\w{6}/"',
        r"world[\w-]*?cricket[\w-]*?\.(co|net|org|in(\W|fo)|us)",
        r"(credit|online)[\w-]*?loan[\w-]*?\.(co|net|org|in(\W|fo)|us)",
        r"worldcup\d+live\.(com?|net|org|in(\W|fo)|us)",
        r"((concrete|beton)-?mixer|crusher)[\w-]*?\.(co|net)",
        r"\w{7}formac\.(com|net|org)",
        r"sex\.(com|net|info)", r"https?://(www\.)?sex",
        r"[\w-]{12}\.(webs|66ghz)\.com", r'online\.us[/"<]',
        r"ptvsports\d+.com",
        r"youth\Wserum",
        r"buyviewsutube",
        r"(?:celebrity-?)?net-?worth", "richestcelebrities",
        r"ufc\wfight\wnight"  # Chiesa vs Lee spam
    ]
    city_list = [
        "Agra", "Amritsar", "Bangalore", "Bhopal", "Chandigarh",
        "Chennai", "Coimbatore", "Delhi", "Dubai", "Durgapur",
        "Ghaziabad", "Hyderabad", "Jaipur", "Jalandhar", "Kolkata",
        "Ludhiana", "Mumbai", "Madurai", "Patna", "Portland",
        "Rajkot", "Surat", "Telangana", "Udaipur", "Uttarakhand",
        "Noida", "Pune", "Rohini",
        # yes, these aren't cities but...
        "India", "Pakistan",
        # buyabans.com spammer uses creative variations
        "Sri Lanka", "Srilanka", "Srilankan",
    ]
    rules = [
        # Sites in sites[] will be excluded if 'all' == True.  Whitelisted if 'all' == False.
        #
        # Category: Bad keywords
        # The big list of bad keywords, for titles and posts
        {'regex': r"(?is)\b({})\b|{}".format("|".join(GlobalVars.bad_keywords), "|".join(bad_keywords_nwb)),
         'all': True, 'sites': [], 'reason': "bad keyword in {}", 'title': True, 'body': True, 'username': True,
         'stripcodeblocks': False, 'body_summary': True, 'max_rep': 4, 'max_score': 1},
        # The small list of *potentially* bad keywords, for titles and posts
        {'regex': r'(?is)\b({})\b'.format('|'.join(GlobalVars.watched_keywords.keys())),
         'reason': 'potentially bad keyword in {}',
         'all': True, 'sites': [], 'title': True, 'body': True, 'username': True,
         'stripcodeblocks': False, 'body_summary': True, 'max_rep': 30, 'max_score': 1},
        # Pattern-matching product name: three keywords in a row at least once, or two in a row at least twice
        {'method': pattern_product_name, 'all': True, 'sites': [], 'reason': "pattern-matching product name in {}",
         'title': True, 'body': True, 'username': False, 'stripcodeblocks': True, 'body_summary': True,
         'answers': False, 'max_rep': 4, 'max_score': 1},
        # "What is this" - pharma title
        {'method': what_is_this_pharma_title, 'all': True, 'sites': [], 'reason': 'Pattern-matching title',
         'title': True, 'body': False, 'username': False, 'stripcodeblocks': False, 'body_summary': False,
         'answers': False, 'max_rep': 1, 'max_score': 1},
        # gratis at the beginning of post, SoftwareRecs is exempt
        {'regex': r"(?is)^.{0,200}\bgratis\b$", 'all': True,
         'sites': ['softwarerecs.stackexchange.com'], 'reason': "bad keyword in {}", 'title': True, 'body': True,
         'username': False, 'stripcodeblocks': False, 'body_summary': True, 'max_rep': 11, 'max_score': 0},
        # Bad keywords in titles and usernames, all sites
        {'regex': r"(?i)^(?:(?=.*?\b(?:online|hd)\b)(?=.*?(?:free|full|unlimited)).*?movies?\b)|(?=.*?\b(?:acai|"
                  r"kisn)\b)(?=.*?care).*products?\b|(?=.*?packer).*mover|(online|certification).*?training|"
                  r"\bvs\b.*\b(live|vivo)\b|(?<!can |uld )\bwe offer\b|payday loan|смотреть.*онлайн|"
                  r"watch\b.{0,50}(online|episode|free)|episode.{0,50}\bsub\b", 'all': True,
         'sites': [], 'reason': "bad keyword in {}", 'title': True, 'body': False, 'username': True,
         'stripcodeblocks': False, 'body_summary': False, 'max_rep': 1, 'max_score': 0},
        # Bad keywords in titles only, all sites
        {'regex': r"(?i)\b(?!s.m.a.r.t)[a-z]\.+[a-z]\.+[a-z]\.+[a-z]\.+[a-z]\b", 'all': True,
         'sites': [], 'reason': "bad keyword in {}", 'title': True, 'body': False, 'username': False,
         'stripcodeblocks': False, 'body_summary': False, 'max_rep': 1, 'max_score': 0},
        {'regex': r'(?i)[\w\s]{0,20}help(?: a)?(?: weak)? postgraduate student(?: to)? write(?: a)? book\??',
         'all': True, 'sites': [], 'reason': 'bad keyword in {}', 'title': True, 'body': False, 'username': False,
         'stripcodeblocks': False, 'body_summary': False, 'max_rep': 20, 'max_score': 2},
        # Eltima: separated into its own method so we can constrain length
        {'method': has_eltima, 'all': True, 'sites': [], 'reason': "bad keyword in {}", 'title': False, 'body': True,
         'username': False, 'stripcodeblocks': False, 'body_summary': False, 'max_rep': 50, 'max_score': 0},
        # Fake-customer-service in title
        {'method': has_customer_service, 'all': True, 'sites': [], 'reason': "bad keyword in {}", 'title': True,
         'body': True, 'username': False, 'stripcodeblocks': False, 'body_summary': False, 'questions': False,
         'max_rep': 1, 'max_score': 0},
        # Bad health-related keywords in titles, health sites are exempt
        {'regex': r"(?i)\b((beauty|skin|health|face|eye)[- ]?(serum|therapy|hydration|tip|renewal|shop|store|lyft|"
                  r"product|strateg(y|ies)|gel|lotion|cream|treatment|method|school|expert)|fat ?burn(er|ing)?|"
                  r"muscle|testo ?[sx]\w*|body ?build(er|ing)|wrinkle|probiotic|acne|peni(s|le)|erection)s?\b|"
                  r"(beauty|skin) care\b", 'all': True,
         'sites': ["fitness.stackexchange.com", "biology.stackexchange.com", "health.stackexchange.com",
                   "skeptics.stackexchange.com", "robotics.stackexchange.com"], 'reason': "bad keyword in {}",
         'title': True, 'body': False, 'username': False, 'stripcodeblocks': False, 'body_summary': False,
         'max_rep': 1, 'max_score': 0},
        # Bad health-related keywords in titles, health sites are exempt, flexible method
        {'method': has_health, 'all': False,
         'sites': ["stackoverflow.com", "superuser.com", "askubuntu.com", "drupal.stackexchange.com",
                   "meta.stackexchange.com", "security.stackexchange.com", "webapps.stackexchange.com",
                   "apple.stackexchange.com", "graphicdesign.stackexchange.com", "workplace.stackexchange.com",
                   "patents.stackexchange.com", "money.stackexchange.com", "gaming.stackexchange.com",
                   "arduino.stackexchange.com"],
         'reason': "bad keyword in {}", 'title': True, 'body': False, 'username': False, 'stripcodeblocks': False,
         'body_summary': False, 'max_rep': 1, 'max_score': 0},
        # Bad health-related keywords in titles and posts, health sites are exempt
        {'regex': r"(?is)virility|diet ?(plan|pill)|\b(pro)?derma(?=[a-su-z ]\w)|(fat|(?<!dead[ -]?)weight)"
                  r"[ -]?(loo?s[es]|reduction)|loo?s[es] ?weight|erectile|\bherpes\b|colon ?(detox|clean)|\bpenis\b",
         'all': True,
         'sites': ["fitness.stackexchange.com", "biology.stackexchange.com", "health.stackexchange.com",
                   "skeptics.stackexchange.com", "bicycles.stackexchange.com", "islam.stackexchange.com",
                   "pets.stackexchange.com"],
         'reason': "bad keyword in {}", 'title': True, 'body': True, 'username': False, 'stripcodeblocks': True,
         'body_summary': True, 'max_rep': 1, 'max_score': 0},
        # Korean character in title: requires 3
        {'regex': r"(?i)\p{Script=Hangul}.*\p{Script=Hangul}.*\p{Script=Hangul}", 'all': True,
         'sites': ["korean.stackexchange.com"], 'reason': "Korean character in {}", 'title': True, 'body': False,
         'username': False, 'stripcodeblocks': False, 'body_summary': False, 'max_rep': 1, 'max_score': 0},
        # Chinese characters in title: requires 3
        {'regex': r"(?i)\p{Script=Han}.*\p{Script=Han}.*\p{Script=Han}", 'all': True,
         'sites': ["chinese.stackexchange.com", "japanese.stackexchange.com", "ja.stackoverflow.com"],
         'reason': "Chinese character in {}", 'title': True, 'body': False, 'username': False, 'stripcodeblocks': False,
         'body_summary': False, 'max_rep': 1, 'max_score': 0},
        # Hindi character in title
        {'regex': r"(?i)\p{Script=Devanagari}", 'all': True,
         'sites': ["hinduism.stackexchange.com"], 'reason': "Hindi character in {}", 'title': True, 'body': False,
         'username': False, 'stripcodeblocks': False, 'body_summary': False, 'max_rep': 1, 'max_score': 0},
        # English text on non-English site: rus.SE only
        {'regex': r"(?i)^[a-z0-9_\W]*[a-z]{3}[a-z0-9_\W]*$", 'all': False,
         'sites': ["rus.stackexchange.com"], 'reason': "English text in {} on a localized site", 'title': True,
         'body': True, 'username': False, 'stripcodeblocks': True, 'body_summary': False, 'max_rep': 1, 'max_score': 0},
        # Roof repair
        {'regex': u"roof repair", 'all': True,
         'sites': ["diy.stackexchange.com", "outdoors.stackexchange.com", "mechanics.stackexchange.com"],
         'reason': "bad keyword in {}", 'title': True, 'body': True, 'username': False, 'stripcodeblocks': True,
         'body_summary': True, 'max_rep': 11, 'max_score': 0},
        # Bad keywords (only include link at end sites + SO, the other sites give false positives for these keywords)
        {'regex': r"(?i)(?<!truth )serum|\b(?<!to )supplements\b", 'all': False,
         'sites': ["stackoverflow.com", "superuser.com", "askubuntu.com", "drupal.stackexchange.com",
                   "meta.stackexchange.com", "security.stackexchange.com", "webapps.stackexchange.com",
                   "apple.stackexchange.com", "graphicdesign.stackexchange.com", "workplace.stackexchange.com",
                   "patents.stackexchange.com", "money.stackexchange.com", "gaming.stackexchange.com",
                   "arduino.stackexchange.com"],
         'reason': "bad keyword in {}", 'title': True, 'body': True, 'username': False, 'stripcodeblocks': True,
         'body_summary': True, 'max_rep': 1, 'max_score': 0},
        # Mostly non-Latin alphabet
        {'method': mostly_non_latin, 'all': True,
         'sites': ["stackoverflow.com", "ja.stackoverflow.com", "pt.stackoverflow.com", "es.stackoverflow.com",
                   "islam.stackexchange.com", "japanese.stackexchange.com", "anime.stackexchange.com",
                   "hinduism.stackexchange.com", "judaism.stackexchange.com", "buddhism.stackexchange.com",
                   "chinese.stackexchange.com", "french.stackexchange.com", "spanish.stackexchange.com",
                   "portuguese.stackexchange.com", "codegolf.stackexchange.com", "korean.stackexchange.com",
                   "ukrainian.stackexchange.com"],
         'reason': 'mostly non-Latin {}', 'title': True, 'body': True, 'username': False, 'stripcodeblocks': True,
         'body_summary': True, 'max_rep': 1, 'max_score': 0},
        # Mostly non-Latin alphabet, SO answers only
        {'method': mostly_non_latin, 'all': False,
         'sites': ["stackoverflow.com"],
         'reason': 'mostly non-Latin {}', 'title': False, 'body': True, 'username': False, 'stripcodeblocks': True,
         'body_summary': True, 'questions': False, 'max_rep': 1, 'max_score': 0},
        # Jesus Christ, the Son of God, on SciFi.
        {'regex': r"Son of (David|man)", 'all': False,
         'sites': ["scifi.stackexchange.com"],
         'reason': "bad keyword in {}", 'title': False, 'body': False, 'username': True, 'stripcodeblocks': False,
         'body_summary': False, 'max_rep': 1, 'max_score': 0},
        {'regex': r"holocaust\W(witnesses|belie(f|vers?)|denier)", 'all': False,
         'sites': ["skeptics.stackexchange.com", "history.stackexchange.com"],
         'reason': "bad keyword in {}", 'title': True, 'body': True, 'username': False, 'stripcodeblocks': False,
         'body_summary': False, 'max_rep': 1, 'max_score': 0},
        {'method': mevaqesh_troll, 'all': False, 'sites': ['judaism.stackexchange.com'],
         'reason': 'potential troll victim', 'title': False, 'body': True, 'username': False,
         'stripcodeblocks': False, 'body_summary': False, 'max_rep': 11, 'max_score': 1},
        #
        # Category: Suspicious links
        # Blacklisted sites
        {'regex': u"(?i)({})".format("|".join(GlobalVars.blacklisted_websites)), 'all': True,
         'sites': [], 'reason': "blacklisted website in {}", 'title': True, 'body': True, 'username': False,
         'stripcodeblocks': False, 'body_summary': True, 'max_rep': 50, 'max_score': 5},
        # Suspicious sites
        {'regex': r"(?i)({}|({})[\w-]*?\.(co|net|org|in(\W|fo)|us|blogspot|wordpress))(?![^>]*<)".format(
            "|".join(pattern_websites), "|".join(bad_keywords_nwb)), 'all': True,
         'sites': [], 'reason': "pattern-matching website in {}", 'title': True, 'body': True, 'username': False,
            'stripcodeblocks': True, 'body_summary': True, 'max_rep': 1, 'max_score': 1},
        # Bad keyword in link text
        {'method': bad_link_text, 'all': True,
         'sites': [], 'reason': 'bad keyword in link text in {}', 'title': False, 'body': True, 'username': False,
         'stripcodeblocks': True, 'body_summary': False, 'max_rep': 1, 'max_score': 0},
        # Bad fragment in link
        {'method': bad_pattern_in_url, 'all': True, 'sites': [],
         'reason': 'bad pattern in URL {}',
         'title': False, 'body': True, 'username': False,
         'stripcodeblocks': True, 'body_summary': True,
         'max_rep': 1, 'max_score': 0},
        # Link has NS hosted by bad guy
        {'method': bad_ns_for_url_domain, 'all': True, 'sites': [],
         'reason': 'bad NS for domain in {}',
         'title': True, 'body': True, 'username': False,
         'stripcodeblocks': True, 'body_summary': True,
         'max_rep': 1, 'max_score': 0},
        # Country-name domains, travel and expats sites are exempt
        {'regex': r"(?i)([\w-]{6}|shop)(australia|brazil|canada|denmark|france|india|mexico|norway|pakistan|"
                  r"spain|sweden)\w{0,4}\.(com|net)", 'all': True,
         'sites': ["travel.stackexchange.com", "expatriates.stackexchange.com"],
         'reason': "pattern-matching website in {}", 'title': True, 'body': True, 'username': True,
         'stripcodeblocks': False, 'body_summary': True, 'max_rep': 1, 'max_score': 0},
        # The TLDs of Iran, Pakistan, and Tokelau in answers
        {'regex': r'(?i)http\S*\.(ir|pk|tk)[/"<]', 'all': True,
         'sites': [], 'reason': "pattern-matching website in {}", 'title': True, 'body': True, 'username': True,
         'stripcodeblocks': False, 'body_summary': True, 'max_rep': 1, 'max_score': 0, 'questions': False},
        # Suspicious health-related websites, health sites are exempt
        {'regex': r"(?i)(bodybuilding|workout|fitness(?!e)|diet|perfecthealth|muscle|nutrition|"
                  r"prostate)[\w-]*?\.(com|co\.|net|org|info|in\W)", 'all': True,
         'sites': ["fitness.stackexchange.com", "biology.stackexchange.com", "health.stackexchange.com",
                   "skeptics.stackexchange.com", "bicycles.stackexchange.com"],
         'reason': "pattern-matching website in {}", 'title': True, 'body': True, 'username': True,
         'stripcodeblocks': False, 'body_summary': True, 'max_rep': 4, 'max_score': 2},
        # Links preceded by arrows >>>
        {'regex': r"(?is)(>>>+|==\s*>>+|====|===>+|==>>+|= = =|(Read More|Click Here) \W{2,20}).{0,20}"
                  r"http(?!://i.stack.imgur.com).{0,200}$", 'all': True,
         'sites': [], 'reason': "link following arrow in {}", 'title': True, 'body': True, 'username': True,
         'stripcodeblocks': True, 'body_summary': False, 'answers': False, 'max_rep': 11, 'max_score': 0},
        # Link at the end of question, selected sites
        {'method': link_at_end, 'all': False,
         'sites': ["superuser.com", "askubuntu.com", "drupal.stackexchange.com", "meta.stackexchange.com",
                   "security.stackexchange.com", "patents.stackexchange.com", "money.stackexchange.com",
                   "gaming.stackexchange.com", "arduino.stackexchange.com", "workplace.stackexchange.com"],
         'reason': 'link at end of {}', 'title': False, 'body': True, 'username': False, 'stripcodeblocks': False,
         'body_summary': False, 'answers': False, 'max_rep': 1, 'max_score': 0},
        # Link at the end of a short answer
        {'regex': r'(?is)^.{0,350}<a href="https?://(?:(?:www\.)?[\w-]+\.(?:blogspot\.|wordpress\.|co\.)?\w{2,4}'
                  r'/?\w{0,2}/?|(?:plus\.google|www\.facebook)\.com/[\w/]+)"[^<]*</a>(?:</strong>)?\W*</p>\s*$'
                  r'|\[/url\]\W*</p>\s*$',
         'all': True, 'sites': ["raspberrypi.stackexchange.com", "softwarerecs.stackexchange.com"],
         'reason': 'link at end of {}', 'title': False, 'body': True, 'username': False, 'stripcodeblocks': False,
         'body_summary': False, 'questions': False, 'max_rep': 1, 'max_score': 0},
        # URL repeated at end of post
        {'regex': r"(?s)<a href=\"(?:http://%20)?(https?://(?:(?:www\.)?"
            r"[\w-]+\.(?:blogspot\.|wordpress\.|co\.)?\w{2,10}/?"
            r"[\w-]{0,40}?/?|(?:plus\.google|www\.facebook)\.com/[\w/]+))"
            r"\" rel=\"nofollow( noreferrer)?\">"
            r".{300,}<a href=\"(?:http://%20)?\1\" "
            r"rel=\"nofollow( noreferrer)?\">(?:http://%20)?\1</a>"
            r"(?:</strong>)?\W*</p>\s*$", 'all': True,
         'sites': [], 'reason': 'repeated URL at end of long post', 'title': False, 'body': True, 'username': False,
         'body_summary': False, 'stripcodeblocks': True, 'max_rep': 1, 'max_score': 0},
        # Link with "thanks for sharing" or a similar phrase in a short answer
        {'method': keyword_link, 'all': True,
         'sites': [], 'reason': 'bad keyword with a link in {}', 'title': False, 'body': True, 'username': False,
         'stripcodeblocks': False, 'body_summary': False, 'questions': False, 'max_rep': 1, 'max_score': 0},
        # non-linked .tk site at the end of an answer
        {'regex': r'(?is)\w{3}\.tk(?:</strong>)?\W*</p>\s*$', 'all': True,
         'sites': [], 'reason': 'pattern-matching website in {}', 'title': False, 'body': True, 'username': False,
         'stripcodeblocks': False, 'body_summary': False, 'questions': False, 'max_rep': 1, 'max_score': 0},
        # non-linked site at the end of a short answer
        {'regex': r'(?is)^.{0,350}\w{6}\.(com|co\.uk)(?:</strong>)?\W*</p>\s*$', 'all': True,
         'sites': [], 'reason': 'link at end of {}', 'title': False, 'body': True, 'username': False,
         'stripcodeblocks': False, 'body_summary': False, 'questions': False, 'max_rep': 1, 'max_score': 0},
        # Shortened URL near the end of question
        {'regex': r"(?is)://(goo\.gl|bit\.ly|bit\.do|tinyurl\.com|fb\.me|cl\.ly|t\.co|is\.gd|j\.mp|tr\.im|ow\.ly|"
                  r"wp\.me|alturl\.com|tiny\.cc|9nl\.me|post\.ly|dyo\.gs|bfy\.tw|amzn\.to|adf\.ly|adfoc\.us|"
                  r"surl\.cn\.com|clkmein\.com|bluenik\.com|rurl\.us|adyou\.co)/.{0,200}$", 'all': True,
         'sites': ["superuser.com", "askubuntu.com"], 'reason': "shortened URL in {}", 'title': False, 'body': True,
         'username': False, 'stripcodeblocks': True, 'body_summary': False, 'answers': False, 'max_rep': 1,
         'max_score': 0},
        # Shortened URL in an answer
        {'regex': r"(?is)://(goo\.gl|bit\.ly|bit\.do|tinyurl\.com|fb\.me|cl\.ly|t\.co|is\.gd|j\.mp|tr\.im|ow\.ly|"
                  r"wp\.me|alturl\.com|tiny\.cc|9nl\.me|post\.ly|dyo\.gs|bfy\.tw|amzn\.to|adf\.ly|adfoc\.us|"
                  r"surl\.cn\.com|clkmein\.com|bluenik\.com|rurl\.us|adyou\.co)/",
         'all': True, 'sites': ["codegolf.stackexchange.com"], 'reason': "shortened URL in {}", 'title': False,
         'body': True, 'username': False, 'stripcodeblocks': True, 'body_summary': False, 'questions': False,
         'max_rep': 1, 'max_score': 0},
        # Link text without Latin characters
        {'regex': u">[^0-9A-Za-z<'\"]{3,}</a>", 'all': True,
         'sites': ["ja.stackoverflow.com", "ru.stackoverflow.com", "rus.stackexchange.com", "islam.stackexchange.com",
                   "japanese.stackexchange.com", "hinduism.stackexchange.com", "judaism.stackexchange.com",
                   "buddhism.stackexchange.com", "chinese.stackexchange.com", "russian.stackexchange.com",
                   "codegolf.stackexchange.com", "korean.stackexchange.com", "ukrainian.stackexchange.com"],
         'reason': 'non-Latin link in {}', 'title': False, 'body': True, 'username': False, 'stripcodeblocks': True,
         'body_summary': False, 'questions': False, 'max_rep': 1, 'max_score': 0},
        # Link text with some non-Latin characters, answers only
        {'method': non_english_link, 'all': True, 'sites': ["pt.stackoverflow.com", "es.stackoverflow.com",
                                                            "ja.stackoverflow.com", "ru.stackoverflow.com",
                                                            "rus.stackexchange.com", "islam.stackexchange.com",
                                                            "japanese.stackexchange.com", "hinduism.stackexchange.com",
                                                            "judaism.stackexchange.com", "buddhism.stackexchange.com",
                                                            "chinese.stackexchange.com", "russian.stackexchange.com",
                                                            "french.stackexchange.com", "portuguese.stackexchange.com",
                                                            "spanish.stackexchange.com", "codegolf.stackexchange.com",
                                                            "korean.stackexchange.com", "esperanto.stackexchange.com",
                                                            "ukrainian.stackexchange.com"],
         'reason': 'non-English link in {}', 'title': False, 'body': True, 'username': False, 'stripcodeblocks': True,
         'body_summary': False, 'questions': False, 'max_rep': 1, 'max_score': 0},
        # Link text is one character within a word
        {'regex': r'(?iu)\w<a href="[^"]+" rel="nofollow( noreferrer)?">.</a>\w', 'all': True,
         'sites': [], 'reason': 'one-character link in {}', 'title': False, 'body': True, 'username': False,
         'stripcodeblocks': True, 'body_summary': False, 'max_rep': 11, 'max_score': 1},
        # Link text consists of punctuation, answers only
        {'regex': r'(?iu)rel="nofollow( noreferrer)?">(?!><>)\W+</a>', 'all': True,
         'sites': ["codegolf.stackexchange.com"], 'reason': 'linked punctuation in {}', 'title': False, 'body': True,
         'username': False, 'stripcodeblocks': True, 'body_summary': False, 'questions': False, 'max_rep': 11,
         'max_score': 1},
        # URL in title, some sites are exempt
        {'regex': r"(?i)https?://(?!(www\.)?(example|domain)\.(com|net|org))[a-zA-Z0-9_.-]+\.[a-zA-Z]{2,4}|"
                  r"\w{3,}\.(com|net)\b.*\w{3,}\.(com|net)\b", 'all': True,
         'sites': ["stackoverflow.com", "pt.stackoverflow.com", "ru.stackoverflow.com", "es.stackoverflow.com",
                   "ja.stackoverflow.com", "superuser.com", "askubuntu.com", "serverfault.com",
                   "unix.stackexchange.com", "webmasters.stackexchange.com"], 'reason': "URL in title",
         'title': True, 'body': False, 'username': False, 'stripcodeblocks': False, 'body_summary': False,
         'max_rep': 11, 'max_score': 0},
        # URL-only title, for the exempt sites
        {'regex': r"(?i)^https?://(?!(www\.)?(example|domain)\.(com|net|org))[a-zA-Z0-9_.-]+\.[a-zA-Z]{2,4}(/\S*)?$",
         'all': False,
         'sites': ["stackoverflow.com", "pt.stackoverflow.com", "ru.stackoverflow.com", "es.stackoverflow.com",
                   "ja.stackoverflow.com", "superuser.com", "askubuntu.com", "serverfault.com",
                   "unix.stackexchange.com", "webmasters.stackexchange.com"], 'reason': "URL-only title",
         'title': True, 'body': False, 'username': False, 'stripcodeblocks': False, 'body_summary': False,
         'max_rep': 11, 'max_score': 0},
        #
        # Category: Suspicious contact information
        # Phone number in title
        {'method': has_phone_number, 'all': True,
         'sites': ["patents.stackexchange.com", "math.stackexchange.com", "mathoverflow.net"],
         'reason': "phone number detected in {}", 'title': True, 'body': False, 'username': False,
         'stripcodeblocks': True, 'body_summary': False, 'max_rep': 1, 'max_score': 0},
        # Phone number in post
        {'regex': r"(?s)^.{0,250}\b1 ?[-(. ]8\d{2}[-). ] ?\d{3}[-. ]\d{4}\b", 'all': True,
         'sites': ["math.stackexchange.com"], 'reason': "phone number detected in {}", 'title': False,
         'body': True, 'username': False, 'stripcodeblocks': True, 'body_summary': True, 'max_rep': 1, 'max_score': 0},
        # Email check for answers on selected sites
        {'regex': r"(?i)(?<![=#/])\b[A-z0-9_.%+-]+@(?!(example|domain|site|foo|\dx)\.[A-z]{2,4})"
                  r"[A-z0-9_.%+-]+\.[A-z]{2,4}\b", 'all': False,
         'sites': ["biology.stackexchange.com", "bitcoin.stackexchange.com", "ell.stackexchange.com",
                   "english.stackexchange.com", "expatriates.stackexchange.com", "gaming.stackexchange.com",
                   "health.stackexchange.com", "money.stackexchange.com", "parenting.stackexchange.com",
                   "rpg.stackexchange.com", "scifi.stackexchange.com", "travel.stackexchange.com",
                   "worldbuilding.stackexchange.com"], 'reason': "email in {}", 'title': True, 'body': True,
         'username': False, 'stripcodeblocks': True, 'body_summary': False, 'questions': False, 'max_rep': 1,
         'max_score': 0},
        # Email check for questions: check only at the end, and on selected sites
        {'regex': r"(?i)(?<![=#/])\b[A-z0-9_.%+-]+@(?!(example|domain|site|foo|\dx)\.[A-z]{2,4})"
                  r"[A-z0-9_.%+-]+\.[A-z]{2,4}\b(?s).{0,100}$", 'all': False,
         'sites': ["money.stackexchange.com", "travel.stackexchange.com", "gamedev.stackexchange.com",
                   "gaming.stackexchange.com"], 'reason': "email in {}", 'title': True, 'body': True,
         'username': False, 'stripcodeblocks': True, 'body_summary': False, 'answers': False, 'max_rep': 1,
         'max_score': 0},
        # Combination of keyword and email in questions and answers, for all sites
        {'method': keyword_email, 'all': True, 'sites': [], 'reason': "bad keyword with email in {}", 'title': True,
         'body': True, 'username': False, 'stripcodeblocks': True, 'body_summary': False, 'max_rep': 1, 'max_score': 0},
        # Spammy-looking email in questions and answers, for all sites
        {'method': pattern_email, 'all': True, 'sites': [], 'reason': "pattern-matching email in {}", 'title': True,
         'body': True, 'username': False, 'stripcodeblocks': True, 'body_summary': False, 'max_rep': 1, 'max_score': 0},
        # QQ/ICQ/Whatsapp... numbers, for all sites
        {'regex': r'(?i)(?<![a-z0-9])Q{1,2}(?:(?:[vw]|[^a-z0-9])\D{0,8})?\d{5}[.-]?\d{4,5}(?!["\d])|'
                  r'\bICQ[ :]{0,5}\d{9}\b|\bwh?atsapp?[ :]{0,5}\d{10}', 'all': True, 'sites': [],
         'reason': "messaging number in {}", 'title': True, 'body': True, 'username': False, 'stripcodeblocks': True,
         'body_summary': False, 'max_rep': 1, 'max_score': 0},
        #
        # Category: Trolling
        # Offensive content in titles and posts
        {'method': is_offensive_post, 'all': True, 'sites': [], 'reason': "offensive {} detected", 'title': True,
         'body': True, 'username': False, 'stripcodeblocks': True, 'body_summary': True,
         'max_rep': 101, 'max_score': 2},
        # Offensive title: titles are more sensitive
        {'regex': r"(?i)\bfuck|(?<!brain)fuck(ers?|ing)?\b", 'all': True, 'sites': [],
         'reason': "offensive {} detected", 'title': True, 'body': False, 'username': False,
         'stripcodeblocks': True, 'body_summary': False,
         'max_rep': 101, 'max_score': 5},
        # No whitespace, punctuation, or formatting in a post
        {'regex': r"(?i)^<p>[a-z]+</p>\s*$", 'all': True, 'sites': ["codegolf.stackexchange.com",
                                                                    "puzzling.stackexchange.com"],
         'reason': "no whitespace in {}", 'title': False, 'body': True, 'username': False, 'stripcodeblocks': False,
         'body_summary': False, 'max_rep': 1, 'max_score': 0},
        # Numbers-only title
        {'regex': r"^(?=.*[0-9])[^\pL]*$", 'all': True, 'sites': ["math.stackexchange.com"],
         'reason': "numbers-only title", 'title': True, 'body': False, 'username': False, 'stripcodeblocks': False,
         'body_summary': False, 'max_rep': 50, 'max_score': 0},
        # Few unique characters
        {'method': has_few_characters, 'all': True, 'sites': ["pt.stackoverflow.com", "ru.stackoverflow.com",
                                                              "es.stackoverflow.com", "rus.stackexchange.com"],
         'reason': "few unique characters in {}", 'title': False, 'body': True, 'username': False,
         'stripcodeblocks': False, 'body_summary': False, 'max_rep': 10000, 'max_score': 1000000},
        # Repeating characters
        {'method': has_repeating_characters, 'all': True, 'sites': ["pt.stackoverflow.com", "ru.stackoverflow.com",
                                                                    "es.stackoverflow.com", "rus.stackexchange.com",
                                                                    "chinese.stackexchange.com"],
         'reason': "repeating characters in {}", 'title': True, 'body': True, 'username': False,
         'stripcodeblocks': True, 'body_summary': False, 'max_rep': 1000000, 'max_score': 1000000},
        # Repeating words
        {'method': has_repeated_words, 'all': True, 'sites': [], 'reason': "repeating words in {}", 'title': True,
         'body': True, 'username': False, 'stripcodeblocks': True, 'body_summary': False, 'max_rep': 11,
         'max_score': 0},
        # One unique character in title
        {'regex': r"^(.)\1+$", 'all': True, 'sites': [], 'reason': "{} has only one unique char", 'title': True,
         'body': False, 'username': False, 'stripcodeblocks': True, 'body_summary': False, 'max_rep': 1000000,
         'max_score': 1000000},
        # Parenting troll
        {'regex': r"(?i)\b(erica|jeff|er1ca|spam|moderator)\b", 'all': False, 'sites': ["parenting.stackexchange.com"],
         'reason': "bad keyword in {}", 'title': False, 'body': True, 'username': False, 'stripcodeblocks': False,
         'body_summary': True, 'max_rep': 50, 'max_score': 0},
        # Academia kangaroos
        {'regex': r"(?i)kangaroos", 'all': False, 'sites': ["academia.stackexchange.com"],
         'reason': "bad keyword in {}", 'title': True, 'body': True, 'username': False, 'stripcodeblocks': False,
         'body_summary': False, 'max_rep': 1, 'max_score': 0},
        {'regex': r"(?i)\b\<a href=\".{0,25}\.xyz\"( rel=\"nofollow( noreferrer)?\")?\>.{0,15}google.{0,15}\<\/a\>\b",
         'all': True, 'sites': [], 'reason': 'non-Google "google search" link in {}', 'title': False, 'body': True,
         'username': False, 'body_summary': False, 'stripcodeblocks': True, 'max_rep': 1, 'max_score': 0},
        # Academia image by low-rep user
        {'regex': r'\<img src="[^"]+"(?: alt="[^"]+")?>', 'all': False, 'sites': ['academia.stackexchange.com'],
         'reason': 'image by low-rep user', 'title': False, 'body': True, 'username': False, 'body_summary': False,
         'stripcodeblocks': True, 'max_rep': 1, 'max_score': 0},
        # Link inside nested blockquotes
        {'regex': r'(?:<blockquote>\s*){6}<p><a href="([^<>]+)"[^<>]*>\1</a>\s*</p>\s*</blockquote>', 'all': True,
         'sites': [], 'reason': 'link inside deeply nested blockquotes', 'title': False, 'body': True,
         'username': False, 'body_summary': False, 'stripcodeblocks': True, 'max_rep': 1, 'max_score': 0},
        # Mostly dots in post
        {'method': mostly_dots, 'all': True, 'sites': ['codegolf.stackexchange.com'],
         'reason': 'mostly dots in {}', 'title': True, 'body': True, 'username': False, 'body_summary': False,
         'stripcodeblocks': True, 'max_rep': 50, 'max_score': 0},

        #
        # Category: other
        # Blacklisted usernames
        {'regex': r"(?i)({})".format("|".join(GlobalVars.blacklisted_usernames)), 'all': True, 'sites': [],
         'reason': "blacklisted username", 'title': False, 'body': False, 'username': True, 'stripcodeblocks': False,
         'body_summary': False, 'max_rep': 1, 'max_score': 0},
        {'regex': u"(?i)^jeff$", 'all': False, 'sites': ["parenting.stackexchange.com"],
         'reason': "blacklisted username", 'title': False, 'body': False, 'username': True,
         'stripcodeblocks': False, 'body_summary': False, 'max_rep': 1, 'max_score': 0},

        # User name similar to link
        {'method': username_similar_website, 'all': True, 'sites': [], 'reason': "username similar to website in {}",
         'title': False, 'body': True, 'username': False, 'stripcodeblocks': False, 'body_summary': True,
         'max_rep': 50, 'max_score': 0, 'questions': False},

        # Answer similar to existing answer on post
        {'method': similar_answer, 'all': True, 'sites': ["codegolf.stackexchange.com"],
         'reason': "answer similar to existing answer on post", 'whole_post': True,
         'title': False, 'body': False, 'username': False, 'stripcodeblocks': False,
         'max_rep': 50, 'max_score': 0},

        # A single character is utilized in a high percentage of the post
        {'method': character_utilization_ratio, 'all': False, 'sites': ["judaism.stackexchange.com"],
         'reason': "single character over used in post",
         'title': False, 'body': True, 'username': False, 'stripcodeblocks': False, 'body_summary': True,
         'max_rep': 20, 'max_score': 0},
    ]

    @staticmethod
    def test_post(post):
        result = []
        why = {'title': [], 'body': [], 'username': []}
        for rule in FindSpam.rules:
            body_to_check = post.body
            is_regex_check = 'regex' in rule
            try:
                check_if_answer = rule['answers']
            except KeyError:
                check_if_answer = True
            try:
                check_if_question = rule['questions']
            except KeyError:
                check_if_question = True
            body_to_check = regex.sub("[\xad\u200b\u200c]", "", body_to_check)
            if rule['stripcodeblocks']:
                # use a placeholder to avoid triggering "few unique characters" when most of post is code
                body_to_check = regex.sub("(?s)<pre>.*?</pre>",
                                          u"<pre><code>placeholder for omitted code/код block</pre></code>",
                                          body_to_check)
                body_to_check = regex.sub("(?s)<code>.*?</code>",
                                          u"<pre><code>placeholder for omitted code/код block</pre></code>",
                                          body_to_check)
            if rule['reason'] == 'Phone number detected in {}':
                body_to_check = regex.sub("<img[^>]+>", "", body_to_check)
                body_to_check = regex.sub("<a[^>]+>", "", body_to_check)
            if rule['all'] != (post.post_site in rule['sites']) and post.owner_rep <= rule['max_rep'] and \
                    post.post_score <= rule['max_score']:
                matched_body = None
                compiled_regex = None
                if is_regex_check:
                    compiled_regex = regex.compile(rule['regex'], regex.UNICODE, city=FindSpam.city_list)
                    # using a named list \L in some regexes
                    matched_title = compiled_regex.findall(post.title)
                    matched_username = compiled_regex.findall(post.user_name)
                    if (not post.body_is_summary or rule['body_summary']) and \
                            (not post.is_answer or check_if_answer) and \
                            (post.is_answer or check_if_question):
                        matched_body = compiled_regex.findall(body_to_check)
                else:
                    assert 'method' in rule

                    if 'whole_post' in rule and rule['whole_post']:
                        matched_title, matched_username, matched_body, why_post = rule['method'](post)

                        if matched_title:
                            why["title"].append(u"Title - {}".format(why_post))
                            result.append(rule['reason'].replace("{}", "title"))
                        elif matched_username:
                            why["username"].append(u"Username - {}".format(why_post))
                            result.append(rule['reason'].replace("{}", "username"))
                        elif matched_body:
                            why["body"].append(u"Post - {}".format(why_post))
                            result.append(rule['reason'].replace("{}", "body"))
                    else:
                        matched_title, why_title = rule['method'](post.title, post.post_site, post.user_name)
                        if matched_title and rule['title']:
                            why["title"].append(u"Title - {}".format(why_title))
                        matched_username, why_username = rule['method'](post.user_name, post.post_site, post.user_name)
                        if matched_username and rule['username']:
                            why["username"].append(u"Username - {}".format(why_username))
                        if (not post.body_is_summary or rule['body_summary']) and \
                                (not post.is_answer or check_if_answer) and \
                                (post.is_answer or check_if_question):
                            matched_body, why_body = rule['method'](body_to_check, post.post_site, post.user_name)
                            if matched_body and rule['body']:
                                why["body"].append(u"Post - {}".format(why_body))
                if matched_title and rule['title']:
                    why["title"].append(FindSpam.generate_why(compiled_regex, post.title, u"Title", is_regex_check))
                    result.append(rule['reason'].replace("{}", "title"))
                if matched_username and rule['username']:
                    why["username"].append(FindSpam.generate_why(compiled_regex, post.user_name, u"Username",
                                                                 is_regex_check))
                    result.append(rule['reason'].replace("{}", "username"))
                if matched_body and rule['body']:
                    why["body"].append(FindSpam.generate_why(compiled_regex, body_to_check, u"Body", is_regex_check))
                    type_of_post = "answer" if post.is_answer else "body"
                    result.append(rule['reason'].replace("{}", type_of_post))
        result = list(set(result))
        result.sort()
        why = "\n".join(chain(filter(None, why["title"]), filter(None, why["body"]),
                              filter(None, why["username"]))).strip()
        return result, why

    @staticmethod
    def generate_why(compiled_regex, matched_text, type_of_text, is_regex_check):
        if is_regex_check:
            matches = compiled_regex.finditer(matched_text)
            why_for_matches = []
            for match in matches:
                span = match.span()
                group = match.group()
                why_for_matches.append(u"Position {}-{}: {}".format(span[0] + 1, span[1] + 1, group))
            return type_of_text + u" - " + ", ".join(why_for_matches)
        return ""
