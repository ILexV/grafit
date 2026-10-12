"""Фейковый граф FalkorDB для тестов поиска/MCP без живой БД.

Узел: (id, label, file_type, source_file, loc, dist). Векторный запрос отдаёт узлы по
возрастанию dist (как queryNodes), запрос рёбер для _rel_map — из словаря rels.
"""


class _RS:
    def __init__(self, rows):
        self.result_set = rows


class FakeGraph:
    def __init__(self, nodes, rels=None, owners=None, supers=None, texts=None):
        self.nodes = sorted(nodes, key=lambda n: n[5])
        self.texts = texts or {}        # id -> node.text (по умолч. уникальный: свёртка копий не сработает)
        self.rels = rels or {}          # id -> set(relation)
        self.owners = owners or {}      # id члена -> имя типа-владельца (ребро method)
        self.supers = supers or {}      # имя типа -> [имена интерфейсов/баз]
        self.vec_calls = []

    def _text(self, nid, label):
        return self.texts.get(nid, f"{label} | body of {nid}")

    def query(self, q, params=None):
        params = params or {}
        if "vector.queryNodes" in q and "k" in params:   # dupes.nearest: (id,label,ft,sf,loc,text,dist)
            self.vec_calls.append(params["k"])
            return _RS([(nid, label, ft, sf, loc, self._text(nid, label), dist)
                        for nid, label, ft, sf, loc, dist in self.nodes[:params["k"]]])
        if "m.source_file = $sf" in q:                    # dupes.file_types — типы файла
            return _RS([])
        if "vector.queryNodes" in q:
            self.vec_calls.append(params["cand"])
            rows = [(nid, label, ft, sf, loc, "", self._text(nid, label), dist)
                    for nid, label, ft, sf, loc, dist in self.nodes[:params["cand"]]]
            return _RS(rows)
        if "fulltext" in q:
            return _RS([])
        if "collect(DISTINCT r.relation)" in q:
            return _RS([(i, list(self.rels[i])) for i in params["ids"] if i in self.rels])
        if "r.relation = 'method' RETURN m.id, c.label" in q:
            return _RS([(i, self.owners[i]) for i in params["ids"] if i in self.owners])
        if "r.relation IN ['implements', 'inherits']" in q:
            return _RS([(a, t) for a in params["ls"] for t in self.supers.get(a, [])])
        if "RETURN DISTINCT r.relation" in q:      # neighbors
            return _RS([])
        raise AssertionError(f"неожиданный запрос: {q}")
