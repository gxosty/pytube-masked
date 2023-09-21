"""Implements a simple wrapper around urlopen."""
import http.client
import json
import logging
import re
import socket
import ssl
import random
import threading # to get thread ids
from functools import lru_cache
from urllib import parse
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from pytube.exceptions import RegexMatchError, MaxRetriesExceeded
from pytube.helpers import regex_search, make_fronted_url, split_redirector_url

logger = logging.getLogger(__name__)
default_range_size = 9437184  # 9MB

last_url = {} # used to store the last url per thread for poisoning 'socket.getaddrinfo'
unverified_context = ssl._create_unverified_context()
_dns_resolver = ("google-public-dns-a.google.com", "216.239.36.36") # Traditional IP addresses (8.8.8.8 & 8.8.8.4) can be blocked
_orig_getaddrinfo = socket.getaddrinfo


def _read_ip_from_dns_answer(json_data):
    ip = json_data["Question"][0]["name"]
    search = True

    while search:
        search = False
        for answer in json_data["Answer"]:
            if ip == answer["name"]:
                ip = answer["data"]
                search = True

    return ip


@lru_cache
def get_dns_ip(domain_name):
    """Resolve a DNS Name using DNS-over-HTTP with Domain Fronting

    :param str domain_name:
        Domain Name to resolve
    :rtype: str
    :returns:
        IP Address of associated DNS Name
    """

    url = "https://www.google.com/resolve?name={}".format(domain_name)
    request = Request(
        url,
        headers = {
            "User-Agent": "Mozilla/5.0",
            "accept-language": "en-US,en",
            "Host" : _dns_resolver[0]
        }
    )
    last_url[threading.get_ident()] = url
    res = urlopen(request, context = unverified_context)
    return _read_ip_from_dns_answer(json.loads(res.read().decode("utf-8")))


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    res = _orig_getaddrinfo(host, port, family, type, proto, flags)
    url = last_url.get(threading.get_ident(), None)

    if not url:
        return res

    if "/resolve" in url:
        res = [list(res[0])]
        addr = _dns_resolver[1]
        res[0][4] = (addr, 443)
    elif "googlevideo.com" in url:
        res = [list(res[0])]
        addr = get_dns_ip(split_redirector_url(url)[0])
        res[0][4] = (addr, 443)

    return res

socket.getaddrinfo = _patched_getaddrinfo


def _execute_request(
    url,
    method=None,
    headers=None,
    data=None,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT
):
    last_url[threading.get_ident()] = url
    front_url, host = make_fronted_url(url)
    base_headers = {"User-Agent": "Mozilla/5.0", "accept-language": "en-US,en"}
    if host is not None:
        base_headers["Host"] = host
    if headers:
        base_headers.update(headers)
    if data:
        # encode data for request
        if not isinstance(data, bytes):
            data = bytes(json.dumps(data), encoding="utf-8")
    if front_url.lower().startswith("http"):
        logger.debug(f"-> Url: {front_url}")
        request = Request(front_url, headers=base_headers, method=method, data=data)
    else:
        raise ValueError("Invalid URL")

    res = urlopen(request, timeout=timeout, context = unverified_context)  # nosec
    return res


def get(url, extra_headers=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
    """Send an http GET request.

    :param str url:
        The URL to perform the GET request for.
    :param dict extra_headers:
        Extra headers to add to the request
    :rtype: str
    :returns:
        UTF-8 encoded string of response
    """
    if extra_headers is None:
        extra_headers = {}
    response = _execute_request(url, headers=extra_headers, timeout=timeout)
    return response.read().decode("utf-8")


def post(url, extra_headers=None, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
    """Send an http POST request.

    :param str url:
        The URL to perform the POST request for.
    :param dict extra_headers:
        Extra headers to add to the request
    :param dict data:
        The data to send on the POST request
    :rtype: str
    :returns:
        UTF-8 encoded string of response
    """
    # could technically be implemented in get,
    # but to avoid confusion implemented like this
    if extra_headers is None:
        extra_headers = {}
    if data is None:
        data = {}
    # required because the youtube servers are strict on content type
    # raises HTTPError [400]: Bad Request otherwise
    extra_headers.update({"Content-Type": "application/json"})
    response = _execute_request(
        url,
        headers=extra_headers,
        data=data,
        timeout=timeout
    )
    return response.read().decode("utf-8")


def seq_stream(
    url,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    max_retries=0
):
    """Read the response in sequence.
    :param str url: The URL to perform the GET request for.
    :rtype: Iterable[bytes]
    """
    # YouTube expects a request sequence number as part of the parameters.
    split_url = parse.urlsplit(url)
    base_url = '%s://%s/%s?' % (split_url.scheme, split_url.netloc, split_url.path)

    querys = dict(parse.parse_qsl(split_url.query))

    # The 0th sequential request provides the file headers, which tell us
    #  information about how the file is segmented.
    querys['sq'] = 0
    url = base_url + parse.urlencode(querys)

    segment_data = b''
    for chunk in stream(url, timeout=timeout, max_retries=max_retries):
        yield chunk
        segment_data += chunk

    # We can then parse the header to find the number of segments
    stream_info = segment_data.split(b'\r\n')
    segment_count_pattern = re.compile(b'Segment-Count: (\\d+)')
    for line in stream_info:
        match = segment_count_pattern.search(line)
        if match:
            segment_count = int(match.group(1).decode('utf-8'))

    # We request these segments sequentially to build the file.
    seq_num = 1
    while seq_num <= segment_count:
        # Create sequential request URL
        querys['sq'] = seq_num
        url = base_url + parse.urlencode(querys)

        yield from stream(url, timeout=timeout, max_retries=max_retries)
        seq_num += 1
    return  # pylint: disable=R1711


def stream(
    url,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    max_retries=0,
    start_byte_pos=0
):
    """Read the response in chunks.
    :param str url: The URL to perform the GET request for.
    :rtype: Iterable[bytes]
    """
    file_size: int = default_range_size  # fake filesize to start
    downloaded = 0
    while downloaded < file_size:
        # stop_pos = min(downloaded + default_range_size, file_size) - 1
        tries = 0

        # Attempt to make the request multiple times as necessary.
        while True:
            # If the max retries is exceeded, raise an exception
            if tries >= 1 + max_retries:
                raise MaxRetriesExceeded()

            # Try to execute the request, ignoring socket timeouts
            try:
                response = _execute_request(
                    url + ("" if start_byte_pos == 0 else f"&range={start_byte_pos}-99999999999"),
                    method="GET",
                    timeout=timeout
                )
            except URLError as e:
                # We only want to skip over timeout errors, and
                # raise any other URLError exceptions
                if isinstance(e.reason, socket.timeout):
                    pass
                else:
                    raise
            except http.client.IncompleteRead:
                # Allow retries on IncompleteRead errors for unreliable connections
                pass
            else:
                # On a successful request, break from loop
                break
            tries += 1

        if file_size == default_range_size:
            try:
                content_range = response.info()["Content-Length"]
                file_size = int(content_range)
                downloaded = start_byte_pos
            except (KeyError, IndexError, ValueError) as e:
                logger.error(e)
        while True:
            chunk = response.read(32768) # 32KB
            if not chunk:
                break
            downloaded += len(chunk)
            yield chunk
    return  # pylint: disable=R1711


@lru_cache()
def filesize(url):
    """Fetch size in bytes of file at given URL

    :param str url: The URL to get the size of
    :returns: int: size in bytes of remote file
    """
    return int(head(url)["content-length"])


@lru_cache()
def seq_filesize(url):
    """Fetch size in bytes of file at given URL from sequential requests

    :param str url: The URL to get the size of
    :returns: int: size in bytes of remote file
    """
    total_filesize = 0
    # YouTube expects a request sequence number as part of the parameters.
    split_url = parse.urlsplit(url)
    base_url = '%s://%s/%s?' % (split_url.scheme, split_url.netloc, split_url.path)
    querys = dict(parse.parse_qsl(split_url.query))

    # The 0th sequential request provides the file headers, which tell us
    #  information about how the file is segmented.
    querys['sq'] = 0
    url = base_url + parse.urlencode(querys)
    response = _execute_request(
        url, method="GET"
    )

    response_value = response.read()
    # The file header must be added to the total filesize
    total_filesize += len(response_value)

    # We can then parse the header to find the number of segments
    segment_count = 0
    stream_info = response_value.split(b'\r\n')
    segment_regex = b'Segment-Count: (\\d+)'
    for line in stream_info:
        # One of the lines should contain the segment count, but we don't know
        #  which, so we need to iterate through the lines to find it
        try:
            segment_count = int(regex_search(segment_regex, line, 1))
        except RegexMatchError:
            pass

    if segment_count == 0:
        raise RegexMatchError('seq_filesize', segment_regex)

    # We make HEAD requests to the segments sequentially to find the total filesize.
    seq_num = 1
    while seq_num <= segment_count:
        # Create sequential request URL
        querys['sq'] = seq_num
        url = base_url + parse.urlencode(querys)

        total_filesize += int(head(url)['content-length'])
        seq_num += 1
    return total_filesize


def head(url):
    """Fetch headers returned http GET request.

    :param str url:
        The URL to perform the GET request for.
    :rtype: dict
    :returns:
        dictionary of lowercase headers
    """
    response_headers = _execute_request(url, method="HEAD").info()
    return {k.lower(): v for k, v in response_headers.items()}
