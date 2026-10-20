# binlog-router-bento

Artykuł o budowie routera CDC w Go: czytanie binloga MySQL przez
[go-mysql](https://github.com/go-mysql-org/go-mysql), rozsyłanie zmian przez
[Bento](https://github.com/warpstreamlabs/bento) na wiele sinków z osobnymi filtrami.

- Źródło: [`binlog-router-bento.md`](binlog-router-bento.md)
- Strona: https://gr4c2-2000.github.io/binlog-router-bento/

## Build

`index.html` jest generowany ze źródła markdown:

```bash
pip install pygments
python3 build.py
```
