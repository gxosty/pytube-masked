# pytube-masked

[*pytube*](https://github.com/pytube/pytube) is a genuine, lightweight, dependency-free Python library (and command-line utility) for downloading YouTube videos.
This repository is fork of [*pytube*](https://github.com/pytube/pytube) and aims to circumvent censorship where YouTube is blocked. The idea behind it is to use [Domain Fronting](https://en.wikipedia.org/wiki/Domain_fronting), probably the most optimal choice to access YouTube.

### Installation

pytube-masked requires an installation of Python 3.6 or greater, as well as pip. (Pip is typically bundled with Python [installations](https://python.org/downloads).)

To install from the source with pip:

```bash
$ python -m pip install git+https://github.com/gxosty/pytube-masked
```

### Using pytube-masked in a Python script

To download a video using the library in a script, you'll need to import the YouTube class from the library and pass an argument of the video URL. From there, you can access the streams and download them.

```python
 >>> from pytube import YouTube
 >>> YouTube('https://youtu.be/2lAe1cqCOXo').streams.first().download()
 >>> yt = YouTube('http://youtube.com/watch?v=2lAe1cqCOXo')
 >>> yt.streams
  ... .filter(progressive=True, file_extension='mp4')
  ... .order_by('resolution')
  ... .desc()
  ... .first()
  ... .download()
```

### Using the command-line interface

Using the CLI is remarkably straightforward as well. To download a video at the highest progressive quality, you can use the following command:
```bash
$ pytube https://youtube.com/watch?v=2lAe1cqCOXo
```

You can also do the same for a playlist:
```bash
$ pytube https://www.youtube.com/playlist?list=PLS1QulWo1RIaJECMeUT4LFwJ-ghgoSH6n
```
