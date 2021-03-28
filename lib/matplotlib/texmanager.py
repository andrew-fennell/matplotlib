r"""
Support for embedded TeX expressions in Matplotlib via dvipng and dvips for the
raster and PostScript backends.  The tex and dvipng/dvips information is cached
in ~/.matplotlib/tex.cache for reuse between sessions.

Requirements:

* LaTeX
* \*Agg backends: dvipng>=1.6
* PS backend: psfrag, dvips, and Ghostscript>=9.0

For raster output, you can get RGBA numpy arrays from TeX expressions
as follows::

  texmanager = TexManager()
  s = "\TeX\ is Number $\displaystyle\sum_{n=1}^\infty\frac{-e^{i\pi}}{2^n}$!"
  Z = texmanager.get_rgba(s, fontsize=12, dpi=80, rgb=(1, 0, 0))

To enable TeX rendering of all text in your Matplotlib figure, set
:rc:`text.usetex` to True.
"""

import functools
import hashlib
import logging
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

import numpy as np

import matplotlib as mpl
from matplotlib import cbook, dviread, rcParams

_log = logging.getLogger(__name__)


class TexManager:
    """
    Convert strings to dvi files using TeX, caching the results to a directory.

    Repeated calls to this constructor always return the same instance.
    """

    # Caches.
    texcache = os.path.join(mpl.get_cachedir(), 'tex.cache')
    grey_arrayd = {}

    font_family = 'serif'
    font_families = ('serif', 'sans-serif', 'cursive', 'monospace')

    font_info = {
        'new century schoolbook': ('pnc', r'\renewcommand{\rmdefault}{pnc}'),
        'bookman': ('pbk', r'\renewcommand{\rmdefault}{pbk}'),
        'times': ('ptm', r'\usepackage{mathptmx}'),
        'palatino': ('ppl', r'\usepackage{mathpazo}'),
        'zapf chancery': ('pzc', r'\usepackage{chancery}'),
        'cursive': ('pzc', r'\usepackage{chancery}'),
        'charter': ('pch', r'\usepackage{charter}'),
        'serif': ('cmr', ''),
        'sans-serif': ('cmss', ''),
        'helvetica': ('phv', r'\usepackage{helvet}'),
        'avant garde': ('pag', r'\usepackage{avant}'),
        'courier': ('pcr', r'\usepackage{courier}'),
        # Loading the type1ec package ensures that cm-super is installed, which
        # is necessary for unicode computer modern.  (It also allows the use of
        # computer modern at arbitrary sizes, but that's just a side effect.)
        'monospace': ('cmtt', r'\usepackage{type1ec}'),
        'computer modern roman': ('cmr', r'\usepackage{type1ec}'),
        'computer modern sans serif': ('cmss', r'\usepackage{type1ec}'),
        'computer modern typewriter': ('cmtt', r'\usepackage{type1ec}')}

    @functools.lru_cache()  # Always return the same instance.
    def __new__(cls):
        Path(cls.texcache).mkdir(parents=True, exist_ok=True)
        return object.__new__(cls)

    def get_font_config(self):
        ff = rcParams['font.family']
        if len(ff) == 1 and ff[0].lower() in self.font_families:
            self.font_family = ff[0].lower()
        else:
            _log.info('font.family must be one of (%s) when text.usetex is '
                      'True. serif will be used by default.',
                      ', '.join(self.font_families))
            self.font_family = 'serif'

        fontconfig = [self.font_family]
        fonts = {}
        for font_family in self.font_families:
            for font in rcParams['font.' + font_family]:
                if font.lower() in self.font_info:
                    fonts[font_family] = self.font_info[font.lower()]
                    _log.debug('family: %s, font: %s, info: %s',
                               font_family, font, self.font_info[font.lower()])
                    break
                else:
                    _log.debug('%s font is not compatible with usetex.', font)
            else:
                _log.info('No LaTeX-compatible font found for the %s font '
                          'family in rcParams. Using default.', font_family)
                fonts[font_family] = self.font_info[font_family]
            fontconfig.append(fonts[font_family][0])
        # Add a hash of the latex preamble to fontconfig so that the
        # correct png is selected for strings rendered with same font and dpi
        # even if the latex preamble changes within the session
        preamble_bytes = self.get_custom_preamble().encode('utf-8')
        fontconfig.append(hashlib.md5(preamble_bytes).hexdigest())

        # The following packages and commands need to be included in the latex
        # file's preamble:
        cmd = [fonts['serif'][1],
               fonts['sans-serif'][1],
               fonts['monospace'][1]]
        if self.font_family == 'cursive':
            cmd.append(fonts['cursive'][1])
        self._font_preamble = '\n'.join([r'\usepackage{type1cm}', *cmd])

        return ''.join(fontconfig)

    def get_basefile(self, tex, fontsize, dpi=None):
        """
        Return a filename based on a hash of the string, fontsize, and dpi.
        """
        s = ''.join([tex, self.get_font_config(), '%f' % fontsize,
                     self.get_custom_preamble(), str(dpi or '')])
        return os.path.join(
            self.texcache, hashlib.md5(s.encode('utf-8')).hexdigest())

    def get_font_preamble(self):
        """
        Return a string containing font configuration for the tex preamble.
        """
        return self._font_preamble

    def get_custom_preamble(self):
        """Return a string containing user additions to the tex preamble."""
        return rcParams['text.latex.preamble']

    def _get_preamble(self):
        return "\n".join([
            r"\documentclass{article}",
            # Pass-through \mathdefault, which is used in non-usetex mode to
            # use the default text font but was historically suppressed in
            # usetex mode.
            r"\newcommand{\mathdefault}[1]{#1}",
            self._font_preamble,
            r"\usepackage[utf8]{inputenc}",
            r"\DeclareUnicodeCharacter{2212}{\ensuremath{-}}",
            # geometry is loaded before the custom preamble as convert_psfrags
            # relies on a custom preamble to change the geometry.
            r"\usepackage[papersize=72in, margin=1in]{geometry}",
            self.get_custom_preamble(),
            # textcomp is loaded last (if not already loaded by the custom
            # preamble) in order not to clash with custom packages (e.g.
            # newtxtext) which load it with different options.
            r"\makeatletter"
            r"\@ifpackageloaded{textcomp}{}{\usepackage{textcomp}}"
            r"\makeatother",
        ])

    def make_tex(self, tex, fontsize):
        """
        Generate a tex file to render the tex string at a specific font size.

        Return the file name.
        """
        basefile = self.get_basefile(tex, fontsize)
        texfile = '%s.tex' % basefile
        fontcmd = {'sans-serif': r'{\sffamily %s}',
                   'monospace': r'{\ttfamily %s}'}.get(self.font_family,
                                                       r'{\rmfamily %s}')

        Path(texfile).write_text(
            r"""
%s
\pagestyle{empty}
\begin{document}
%% The empty hbox ensures that a page is printed even for empty inputs, except
%% when using psfrag which gets confused by it.
\fontsize{%f}{%f}%%
\ifdefined\psfrag\else\hbox{}\fi%%
%s
\end{document}
""" % (self._get_preamble(), fontsize, fontsize * 1.25, fontcmd % tex),
            encoding='utf-8')

        return texfile

    def _run_checked_subprocess(self, command, tex, *, cwd=None):
        _log.debug(cbook._pformat_subprocess(command))
        try:
            report = subprocess.check_output(
                command, cwd=cwd if cwd is not None else self.texcache,
                stderr=subprocess.STDOUT)
        except FileNotFoundError as exc:
            raise RuntimeError(
                'Failed to process string with tex because {} could not be '
                'found'.format(command[0])) from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                '{prog} was not able to process the following string:\n'
                '{tex!r}\n\n'
                'Here is the full report generated by {prog}:\n'
                '{exc}\n\n'.format(
                    prog=command[0],
                    tex=tex.encode('unicode_escape'),
                    exc=exc.output.decode('utf-8'))) from exc
        _log.debug(report)
        return report

    def make_dvi(self, tex, fontsize):
        """
        Generate a dvi file containing latex's layout of tex string.

        Return the file name.
        """
        basefile = self.get_basefile(tex, fontsize)
        dvifile = '%s.dvi' % basefile
        if not os.path.exists(dvifile):
            texfile = self.make_tex(tex, fontsize)
            # Generate the dvi in a temporary directory to avoid race
            # conditions e.g. if multiple processes try to process the same tex
            # string at the same time.  Having tmpdir be a subdirectory of the
            # final output dir ensures that they are on the same filesystem,
            # and thus replace() works atomically.
            with TemporaryDirectory(dir=Path(dvifile).parent) as tmpdir:
                self._run_checked_subprocess(
                    ["latex", "-interaction=nonstopmode", "--halt-on-error",
                     texfile], tex, cwd=tmpdir)
                (Path(tmpdir) / Path(dvifile).name).replace(dvifile)
        return dvifile

    def make_png(self, tex, fontsize, dpi):
        """
        Generate a png file containing latex's rendering of tex string.

        Return the file name.
        """
        basefile = self.get_basefile(tex, fontsize, dpi)
        pngfile = '%s.png' % basefile
        # see get_rgba for a discussion of the background
        if not os.path.exists(pngfile):
            dvifile = self.make_dvi(tex, fontsize)
            cmd = ["dvipng", "-bg", "Transparent", "-D", str(dpi),
                   "-T", "tight", "-o", pngfile, dvifile]
            # When testing, disable FreeType rendering for reproducibility; but
            # dvipng 1.16 has a bug (fixed in f3ff241) that breaks --freetype0
            # mode, so for it we keep FreeType enabled; the image will be
            # slightly off.
            if (getattr(mpl, "_called_from_pytest", False)
                    and mpl._get_executable_info("dvipng").version != "1.16"):
                cmd.insert(1, "--freetype0")
            self._run_checked_subprocess(cmd, tex)
        return pngfile

    def get_grey(self, tex, fontsize=None, dpi=None):
        """Return the alpha channel."""
        if not fontsize:
            fontsize = rcParams['font.size']
        if not dpi:
            dpi = rcParams['savefig.dpi']
        key = tex, self.get_font_config(), fontsize, dpi
        alpha = self.grey_arrayd.get(key)
        if alpha is None:
            pngfile = self.make_png(tex, fontsize, dpi)
            rgba = mpl.image.imread(os.path.join(self.texcache, pngfile))
            self.grey_arrayd[key] = alpha = rgba[:, :, -1]
        return alpha

    def get_rgba(self, tex, fontsize=None, dpi=None, rgb=(0, 0, 0)):
        """Return latex's rendering of the tex string as an rgba array."""
        alpha = self.get_grey(tex, fontsize, dpi)
        rgba = np.empty((*alpha.shape, 4))
        rgba[..., :3] = mpl.colors.to_rgb(rgb)
        rgba[..., -1] = alpha
        return rgba

    def get_text_width_height_descent(self, tex, fontsize, renderer=None):
        """Return width, height and descent of the text."""
        if tex.strip() == '':
            return 0, 0, 0
        dvifile = self.make_dvi(tex, fontsize)
        dpi_fraction = renderer.points_to_pixels(1.) if renderer else 1
        with dviread.Dvi(dvifile, 72 * dpi_fraction) as dvi:
            page, = dvi
        # A total height (including the descent) needs to be returned.
        return page.width, page.height + page.descent, page.descent
