import sys
sys.path.insert(0, r'C:\Users\Creature\Creature Folder\Synced\Project\src')
from gem_price.matching.engine import match_single_gem_product, detect_category
import sqlite3

con = sqlite3.connect(r'C:\Users\Creature\Creature Folder\Synced\Project\code\db\gem_project.db')

gem_id = 2
cur = con.cursor()
row = cur.execute('SELECT id, name, link, price FROM raw_products WHERE source=? AND id=?', ('gem', gem_id)).fetchone()
print('GeM Product:', row)

if row:
    gid, gname, glink, gprice = row
    cat = detect_category(gname, glink, {})
    print('Category:', cat.category, cat.confidence, cat.key_specs)
    
    results = match_single_gem_product(gid, gname, glink, gprice, con, top_k=5, cosine_threshold=0.55)
    print(f'Found {len(results)} matches:')
    for i, r in enumerate(results, 1):
        g = r['gates']
        fname = r['flipkart_name'][:50]
        print(f'  {i}. {fname:50s} | comp={r["composite_score"]:.3f} cos={r["cosine_similarity"]:.3f} attr={r["attribute_overlap"]:.2f} | gates: M={g["model"]} P={g["pack"]} F={g["form"]} | {g["why"]}')

con.close()