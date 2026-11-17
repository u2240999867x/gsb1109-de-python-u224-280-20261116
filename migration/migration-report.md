# Registry digest migration report

- Legacy algorithm: `sha256`
- New algorithm: `sha512`
- Entries in the legacy registry: 10
- Published after byte verification: 10
- Held back as pending: 0

New digests are calculated from the cached bytes themselves only
after each legacy credential has been re-computed from those same
bytes and found to match. The new digests are never derived from the
legacy digests.

## Published entries

| Entry | Cached copies | Size (bytes) | Legacy SHA-256 | New SHA-512 |
| --- | --- | --- | --- | --- |
| `subdir/tiny-data.txt` | data/subdir/tiny-data.txt | 59 | `baee0894dba14b12085eacb204284b97e362f4f3e5a5807693cc90ef415c1b2d` | `e3117ce3572046c0cff33f31d112c95973d372744e27f44d0b058ac13e9bf78e5f94c1eaabe311261e6a95736bff92223fcb2407e128d120252c790b4c44cd39` |
| `tiny-data.txt` | pooch/tests/data/tiny-data.txt, data/tiny-data.txt | 59 | `baee0894dba14b12085eacb204284b97e362f4f3e5a5807693cc90ef415c1b2d` | `e3117ce3572046c0cff33f31d112c95973d372744e27f44d0b058ac13e9bf78e5f94c1eaabe311261e6a95736bff92223fcb2407e128d120252c790b4c44cd39` |
| `large-data.txt` | pooch/tests/data/large-data.txt | 102077 | `98de171fb320da82982e6bf0f3994189fff4b42b23328769afce12bdd340444a` | `06a6cba82e69daca0062fcbba39a374f6e695d24bcbf4044579da1b556700d1f25bd033ea96960a604e7c50137f40ccec64d8dfdfd2741b87378f816cb3b05b0` |
| `tiny-data.zip` | pooch/tests/data/tiny-data.zip | 235 | `0d49e94f07bc1866ec57e7fd1b93a351fba36842ec9b13dd50bf94e8dfa35cbb` | `2051706c41fd5818ae72b547e60dfc92d84455f2d22272397b8bfd04491d01d44e1919bb3b7e2a7ee96c254feafba57d36a133028067bcfa9e0323bc5ea274b2` |
| `store.zip` | pooch/tests/data/store.zip, data/store.zip | 780 | `0498d2a001e71051bbd2acd2346f38da7cbd345a633cb7bf0f8a20938714b51a` | `9cec48d2b636cb46637f6f36ab7f1299bc2f9b20f44953579e43169d5a4007555e43bec4580054ef3a874c39fa0f3905cb6c9220f46a7c5764beb5880fa44538` |
| `tiny-data.tar.gz` | pooch/tests/data/tiny-data.tar.gz | 176 | `41503f083814f43a01a8e9a30c28d7a9fe96839a99727a7fdd0acf7cd5bab63b` | `5f495fb71ba1844a1c2d6bd237fd8c9078536081a263059003f97d89714e7bd1f376d17ecdaaa275dd887d53c27903e83493b2b1fb47350d00937ae0e96dfceb` |
| `store.tar.gz` | pooch/tests/data/store.tar.gz | 243 | `088c7f4e0f1859b1c769bb6065de24376f366374817ede8691a6ac2e49f29511` | `7d00e4460c35c019a2d449604f63a0ad13be8652a4dbb7401ebb9ab21ad2b3592276cfe485666b88ad6065804a4c1e8740086a2ac5352ba79fc27e19cde3c3e7` |
| `tiny-data.txt.bz2` | pooch/tests/data/tiny-data.txt.bz2 | 91 | `753663687a4040c90c8578061867d1df623e6aa8011c870a5dbd88ee3c82e306` | `d789c9941fc5563715199ce0ec35df7484034b7418c995958a3eeb0b46bed4c5aed2a8797335629d7ecf6739c5704687fe931c852dccf93776a461f698afb8ae` |
| `tiny-data.txt.gz` | pooch/tests/data/tiny-data.txt.gz | 91 | `2e2da6161291657617c32192dba95635706af80c6e7335750812907b58fd4b52` | `3190bfa50ada571c34dc16a4a780079482fd0dc210a5e92fa3551ae50c41a72a831603a297239493f0fa64165478c58ab353ed650ab9b577d1a3eeacc294c5b0` |
| `tiny-data.txt.xz` | pooch/tests/data/tiny-data.txt.xz | 116 | `99dcb5c32a6e916344bacb4badcbc2f2b6ee196977d1d8187610c21e7e607765` | `7a851780ff24349c88e66aca1d9006d3b054540dd3d4938f23fe9ff2d0eff2f514307fcc04074825daf619fc1c370278788d89f2908d7bbdf2608e2e1c4232a3` |

## Pending entries

None. Every legacy entry had a complete credential and verified bytes.

## Byte identity across the cache

Each group lists cache paths whose bytes hash to the same legacy digest.
Decompressed and converted products therefore stay traceable to the registered
original bytes.

| SHA-256 of the bytes | Registry entry | Cache paths |
| --- | --- | --- |
| `024d5f0c546af8b272ac241b54cbde41153e7e6bda09858d953e4f93b21c81a4` | - | `pooch/tests/data/registry-spaces.txt` |
| `0498d2a001e71051bbd2acd2346f38da7cbd345a633cb7bf0f8a20938714b51a` | `store.zip` | `pooch/tests/data/store.zip`, `data/store.zip` |
| `088c7f4e0f1859b1c769bb6065de24376f366374817ede8691a6ac2e49f29511` | `store.tar.gz` | `pooch/tests/data/store.tar.gz` |
| `0d49e94f07bc1866ec57e7fd1b93a351fba36842ec9b13dd50bf94e8dfa35cbb` | `tiny-data.zip` | `pooch/tests/data/tiny-data.zip` |
| `19d00bdb48756da06ebf627d4d0b5b0539b7f32b3f684a23afb970bfbf1b8ea1` | - | `pooch/tests/data/registry_comments.txt` |
| `2e2da6161291657617c32192dba95635706af80c6e7335750812907b58fd4b52` | `tiny-data.txt.gz` | `pooch/tests/data/tiny-data.txt.gz` |
| `41503f083814f43a01a8e9a30c28d7a9fe96839a99727a7fdd0acf7cd5bab63b` | `tiny-data.tar.gz` | `pooch/tests/data/tiny-data.tar.gz` |
| `426c19aebe552f85ff39e0f2fa74356cf70aa38519c809e34566fa30c653b0f7` | - | `pooch/tests/data/registry-custom-url.txt` |
| `44d3c7ca1edb427d2b09360aeb21d8066ea3a275d98454fa82101ee9ccc04073` | - | `pooch/tests/data/registry-invalid.txt` |
| `700629ad582327027ee10942da5e71b5d80be9736f1fbfbe25de8e8c69096f95` | - | `pooch/tests/data/registry.txt` |
| `753663687a4040c90c8578061867d1df623e6aa8011c870a5dbd88ee3c82e306` | `tiny-data.txt.bz2` | `pooch/tests/data/tiny-data.txt.bz2` |
| `98de171fb320da82982e6bf0f3994189fff4b42b23328769afce12bdd340444a` | `large-data.txt` | `pooch/tests/data/large-data.txt` |
| `99dcb5c32a6e916344bacb4badcbc2f2b6ee196977d1d8187610c21e7e607765` | `tiny-data.txt.xz` | `pooch/tests/data/tiny-data.txt.xz` |
| `baee0894dba14b12085eacb204284b97e362f4f3e5a5807693cc90ef415c1b2d` | `tiny-data.txt` | `pooch/tests/data/store/subdir/tiny-data.txt`, `pooch/tests/data/store/tiny-data.txt`, `pooch/tests/data/tiny-data.txt`, `data/subdir/tiny-data.txt`, `data/tiny-data.txt` |
