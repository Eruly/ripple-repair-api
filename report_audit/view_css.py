"""검증 표시 페이지 공용 CSS (build_verified_view_page.py 와 run_report_audit.py 가 함께 쓴다)."""

CSS = '''
:root{color-scheme:light;--bg:#f5f6f2;--panel:#fbfbf8;--ink:#171d1a;--ink-2:#4b5550;--ink-3:#7b847f;--rule:#d9ddd6;--rule-soft:#e8ebe5;--acc:#2a78d6;
--fact:#1baf7a;--fid:#1f7a3a;--der:#2a78d6;--lat:#9aa39d;--conf:#b3261e;--narr:#eb6834;
--mono:"JetBrains Mono",ui-monospace,Menlo,monospace;--body:"Source Sans 3",system-ui,sans-serif;--disp:"Newsreader",Georgia,serif}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#15181a;--panel:#1c2023;--ink:#eef1ec;--ink-2:#b9c0ba;--ink-3:#848d87;--rule:#30363a;--rule-soft:#262b2e;--acc:#3987e5;--fact:#199e70;--fid:#4fbf6f;--der:#3987e5;--lat:#6f7873;--conf:#f0655d;--narr:#d95926}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#15181a;--panel:#1c2023;--ink:#eef1ec;--ink-2:#b9c0ba;--ink-3:#848d87;--rule:#30363a;--rule-soft:#262b2e;--acc:#3987e5;--fact:#199e70;--fid:#4fbf6f;--der:#3987e5;--lat:#6f7873;--conf:#f0655d;--narr:#d95926}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--body);font-size:15px;line-height:1.55}
.wrap{max-width:1120px;margin:0 auto;padding:30px 24px 60px}
.eyebrow{font-family:var(--mono);font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3)}
h1{font-family:var(--disp);font-weight:500;font-size:36px;margin:6px 0 8px;letter-spacing:-.01em}
.lede{color:var(--ink-2);max-width:76ch;margin:0 0 16px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;color:var(--ink-2);margin:6px 0 22px}
.legend span{display:inline-flex;align-items:center;gap:6px}.legend i{width:14px;height:14px;border-radius:3px;display:inline-block}
h2{font-family:var(--disp);font-weight:500;font-size:24px;margin:34px 0 6px}
.meta{font-family:var(--mono);font-size:12.5px;color:var(--ink-3);margin-bottom:10px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 12px}
.stat{border:1px solid var(--rule);background:var(--panel);padding:8px 12px;font-size:13px}.stat b{font-family:var(--disp);font-size:20px;display:block;line-height:1.1}
.doc{background:var(--panel);border:1px solid var(--rule);padding:16px 18px;font-size:14px;line-height:1.85;max-height:560px;overflow:auto;font-variant-numeric:tabular-nums}
mark{border-radius:3px;padding:0 3px;color:var(--ink);cursor:help;background:transparent}
.v-fact{background:color-mix(in oklab,var(--fact) 30%,transparent)}
.v-fact\\+id{background:color-mix(in oklab,var(--fid) 35%,transparent);text-decoration:underline;text-decoration-color:var(--fid);text-decoration-thickness:2px}
.v-derived{background:color-mix(in oklab,var(--der) 30%,transparent)}
.v-latent{background:color-mix(in oklab,var(--lat) 35%,transparent)}
.v-none{outline:1px dashed var(--ink-3);outline-offset:1px;color:var(--ink-3)}
.v-conflict,.v-narr-conflict{background:color-mix(in oklab,var(--conf) 35%,transparent);outline:2px solid var(--conf)}
.v-narr{background:color-mix(in oklab,var(--narr) 30%,transparent)}
.v-injected{background:color-mix(in oklab,var(--conf) 25%,transparent);outline:2px dashed var(--conf);outline-offset:1px;font-weight:600}
.tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--bg);font-family:var(--mono);font-size:12px;padding:6px 9px;border-radius:3px;opacity:0;transform:translate(-50%,-125%);white-space:pre-wrap;max-width:520px;z-index:9}
.note{font-size:13.5px;color:var(--ink-2);margin:10px 0 0;max-width:80ch}
'''
