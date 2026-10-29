EAPI=8

PYTHON_COMPAT=( python3_{12..14} )
inherit python-single-r1 systemd

DESCRIPTION="ASUS touchpad NumberPad driver"
HOMEPAGE="https://github.com/asus-linux-drivers/asus-numberpad-driver"
SRC_URI="https://github.com/asus-linux-drivers/asus-numberpad-driver/archive/refs/tags/v${PV}.tar.gz -> ${P}.tar.gz"
LICENSE="GPL-2"
SLOT="0"
KEYWORDS="~amd64"
REQUIRED_USE="${PYTHON_REQUIRED_USE}"
RDEPEND="${PYTHON_DEPS}
	$(python_gen_cond_dep '
		dev-python/libevdev[${PYTHON_USEDEP}]
		dev-python/numpy[${PYTHON_USEDEP}]
		dev-python/pyinotify[${PYTHON_USEDEP}]
		dev-python/pyasyncore[${PYTHON_USEDEP}]
		dev-python/python-periphery[${PYTHON_USEDEP}]
		dev-python/xkbcommon[${PYTHON_USEDEP}]
		dev-python/pywayland[${PYTHON_USEDEP}]
		dev-python/pygobject[${PYTHON_USEDEP}]
		dev-python/dbus-python[${PYTHON_USEDEP}]
	')"

PATCHES=( "${FILESDIR}/${P}-shutdown.patch" )

src_install() {
	insinto /usr/share/${PN}
	doins numberpad.py
	doins -r layouts
	python_doscript "${FILESDIR}/asus-numberpad-driver"
	systemd_douserunit "${FILESDIR}/asus-numberpad-driver.service"
}
