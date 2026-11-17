# Copyright (C) 2026  Christian Ledermann
#
# This library is free software; you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation; either version 2.1 of the License, or (at your option)
# any later version.
#
# This library is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this library; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
"""Property based tests for the gx LatLonQuad ground overlay extension."""

from hypothesis import given
from hypothesis import strategies as st

import fastkml
from tests.base import Lxml
from tests.hypothesis.common import assert_repr_roundtrip
from tests.hypothesis.common import assert_str_roundtrip
from tests.hypothesis.common import assert_str_roundtrip_terse
from tests.hypothesis.common import assert_str_roundtrip_verbose
from tests.hypothesis.strategies import nc_name

_coordinates = st.tuples(
    st.floats(min_value=-180, max_value=180, allow_nan=False, allow_infinity=False),
    st.floats(min_value=-90, max_value=90, allow_nan=False, allow_infinity=False),
)


class TestLxml(Lxml):
    @given(
        id=st.one_of(st.none(), nc_name()),
        target_id=st.one_of(st.none(), nc_name()),
        coords=st.one_of(
            st.none(),
            st.lists(_coordinates, min_size=0, max_size=8),
        ),
    )
    def test_fuzz_lat_lon_quad(
        self,
        id: str | None,
        target_id: str | None,
        coords: list[tuple[float, float]] | None,
    ) -> None:
        lat_lon_quad = fastkml.gx.LatLonQuad(
            id=id,
            target_id=target_id,
            coords=coords,
        )

        assert_repr_roundtrip(lat_lon_quad)
        assert_str_roundtrip(lat_lon_quad)
        assert_str_roundtrip_terse(lat_lon_quad)
        assert_str_roundtrip_verbose(lat_lon_quad)

    @given(
        coords=st.lists(_coordinates, min_size=4, max_size=4),
        longitude=st.floats(
            min_value=-180,
            max_value=180,
            allow_nan=False,
            allow_infinity=False,
        ),
        latitude=st.floats(
            min_value=-90,
            max_value=90,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    def test_fuzz_ground_overlay_quad(
        self,
        coords: list[tuple[float, float]],
        longitude: float,
        latitude: float,
    ) -> None:
        lat_lon_quad = fastkml.gx.LatLonQuad(coords=coords)
        ground_overlay = fastkml.GroundOverlay(lat_lon_quad=lat_lon_quad)

        assert_repr_roundtrip(ground_overlay)
        assert_str_roundtrip(ground_overlay)
        assert_str_roundtrip_terse(ground_overlay)
        assert_str_roundtrip_verbose(ground_overlay)

        position = ground_overlay.image_position(longitude, latitude)
        if position is not None:
            assert 0.0 <= position.x <= 1.0
            assert 0.0 <= position.y <= 1.0
            assert position.declaration is lat_lon_quad
