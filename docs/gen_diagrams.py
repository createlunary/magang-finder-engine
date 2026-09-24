import graphviz

COMMON_ATTRS = dict(
    fontname="Helvetica",
    fontsize="11",
)

# ---------------------------------------------------------------------------
# DIAGRAM 1 — Arsitektur Sistem (High-Level Component Diagram)
# ---------------------------------------------------------------------------
g1 = graphviz.Digraph("arsitektur_sistem", format="png")
g1.attr(rankdir="LR", bgcolor="white", pad="0.4", nodesep="0.45", ranksep="0.7", splines="ortho")
g1.attr("node", fontname="Helvetica", fontsize="11", style="filled")
g1.attr("edge", fontname="Helvetica", fontsize="9", color="#555555")

with g1.subgraph(name="cluster_sumber") as c:
    c.attr(label="Sumber Lowongan Magang", style="rounded,dashed", color="#888888", fontsize="11")
    c.node("site_login", "Situs berbasis login\n(mis. LinkedIn, Glints, Kalibrr)", shape="box", fillcolor="#FCE8E6", color="#D93025")
    c.node("site_bot", "Situs anti-bot / karir kampus", shape="box", fillcolor="#FCE8E6", color="#D93025")
    c.node("site_public", "Situs publik / listing terbuka", shape="box", fillcolor="#E8F0FE", color="#1A73E8")

g1.node("connector", "Custom Browser\nSearch Connector\n(session & auth handling)", shape="box3d", fillcolor="#D2E3FC", color="#1A73E8")

g1.node("rag", "RAG Store\n(embedding + vector DB,\ndedup terhadap data lama)", shape="cylinder", fillcolor="#FEF7E0", color="#F9AB00")

g1.node("local_llm", "Local LLM\n(Ollama - Llama/Qwen, RTX 4060)\nEkstraksi field & filter kasar", shape="box", fillcolor="#E6F4EA", color="#188038")

g1.node("large_llm", "Large LLM (Claude API)\nRanking & penilaian kecocokan\nprofil (reasoning)", shape="box", fillcolor="#E6F4EA", color="#188038")

g1.node("profile", "profile.json\n(skill, minat, preferensi user)", shape="note", fillcolor="#F3E8FD", color="#9334E6")

g1.node("output", "Output\nDigest terurut + notifikasi\n(Telegram/email/dashboard)", shape="box", fillcolor="#FCE8E6", color="#D93025", style="filled,bold")

g1.node("user", "Kamu (Pengguna)", shape="ellipse", fillcolor="#FFFFFF", color="#333333")

g1.edge("site_login", "connector")
g1.edge("site_bot", "connector")
g1.edge("site_public", "connector")
g1.edge("connector", "rag", label="raw content")
g1.edge("rag", "local_llm", label="konten baru\n(belum pernah dilihat)")
g1.edge("local_llm", "rag", label="hasil ekstraksi\n(field terstruktur)")
g1.edge("local_llm", "large_llm", label="kandidat\ntersaring")
g1.edge("profile", "large_llm", label="kriteria\nmatching")
g1.edge("large_llm", "output")
g1.edge("output", "user")

g1.render("/home/claude/magang-finder-plan/diagrams/01_arsitektur_sistem", cleanup=True)

# ---------------------------------------------------------------------------
# DIAGRAM 2 — Alur Kerja Detail (Detailed Workflow / Flowchart)
# ---------------------------------------------------------------------------
g2 = graphviz.Digraph("alur_kerja_detail", format="png")
g2.attr(rankdir="TB", bgcolor="white", pad="0.4", nodesep="0.4", ranksep="0.45")
g2.attr("node", fontname="Helvetica", fontsize="11", style="filled")
g2.attr("edge", fontname="Helvetica", fontsize="9", color="#555555")

def box(name, label, **kw):
    g2.node(name, label, shape="box", style="filled,rounded", fillcolor=kw.get("fill", "#E8F0FE"), color=kw.get("border", "#1A73E8"))

def decision(name, label):
    g2.node(name, label, shape="diamond", fillcolor="#FEF7E0", color="#F9AB00")

def data(name, label):
    g2.node(name, label, shape="cylinder", fillcolor="#FEF7E0", color="#F9AB00")

g2.node("start", "Trigger\n(terjadwal / manual)", shape="oval", fillcolor="#FFFFFF", color="#333333")
box("loop", "Untuk tiap situs\ndalam daftar sumber")
decision("auth_ok", "Sesi/login\nmasih valid?")
box("refresh_auth", "Refresh sesi /\nlogin ulang", fill="#FCE8E6", border="#D93025")
box("scrape", "Crawl & scrape\nhalaman listing")
box("extract", "Ekstraksi field mentah\n(judul, perusahaan, deadline,\nrequirement, link)")
decision("dup", "Sudah ada di\nRAG store?\n(dedup)")
box("skip", "Lewati", fill="#F1F3F4", border="#5F6368")
box("local_classify", "Local LLM: bersihkan,\nstrukturkan, klasifikasi\nkasar (relevan bidang?)", fill="#E6F4EA", border="#188038")
data("store_rag", "Simpan ke RAG\nvector DB")
decision("more_sites", "Masih ada\nsitus lain?")
box("batch", "Kumpulkan kandidat\ntersaring (batch)")
box("large_rank", "Large LLM: cocokkan\nke profile.json,\nberi skor & alasan", fill="#E6F4EA", border="#188038")
decision("threshold", "Skor >\nambang batas?")
box("save_result", "Simpan ke\ndaftar hasil akhir")
box("discard", "Buang / arsipkan", fill="#F1F3F4", border="#5F6368")
box("notify", "Kirim digest terurut\n(Telegram/email/dashboard)", fill="#FCE8E6", border="#D93025")
box("log", "Catat log run\n(untuk evaluasi & metrik)")
g2.node("end", "Selesai —\ntunggu trigger berikutnya", shape="oval", fillcolor="#FFFFFF", color="#333333")

g2.edge("start", "loop")
g2.edge("loop", "auth_ok")
g2.edge("auth_ok", "refresh_auth", label="tidak")
g2.edge("refresh_auth", "scrape")
g2.edge("auth_ok", "scrape", label="ya")
g2.edge("scrape", "extract")
g2.edge("extract", "dup")
g2.edge("dup", "skip", label="ya")
g2.edge("dup", "local_classify", label="tidak")
g2.edge("skip", "more_sites")
g2.edge("local_classify", "store_rag")
g2.edge("store_rag", "more_sites")
g2.edge("more_sites", "loop", label="ya")
g2.edge("more_sites", "batch", label="tidak")
g2.edge("batch", "large_rank")
g2.edge("large_rank", "threshold")
g2.edge("threshold", "save_result", label="ya")
g2.edge("threshold", "discard", label="tidak")
g2.edge("save_result", "notify")
g2.edge("discard", "log")
g2.edge("notify", "log")
g2.edge("log", "end")

g2.render("/home/claude/magang-finder-plan/diagrams/02_alur_kerja_detail", cleanup=True)

print("done")
