EAPI=8

PYTHON_COMPAT=( python3_{12..14} )
DISTUTILS_USE_PEP517=setuptools
PYPI_NO_NORMALIZE=1
inherit distutils-r1 pypi

DESCRIPTION="Pure Python interface to Linux peripheral I/O"
HOMEPAGE="https://github.com/vsergeev/python-periphery"
LICENSE="MIT"
SLOT="0"
KEYWORDS="~amd64"
RESTRICT="test"
