"use strict";
// Source text stays text. Only explicit safe source hyperlinks become anchors.
window.gsmeEvidence = function evidenceText(text) {
  const root=document.createElement("div"); root.className="evidence";
  let fenced=false;
  for(const line of text.split("\n")) {
    if(/^\s*(`{3,}|~{3,})/.test(line)){fenced=!fenced;continue;}
    const heading=!fenced && /^(#{1,6})\s+(.+)$/.exec(line);
    const bold=!fenced && /^(\*\*|__)(.+)\1$/.exec(line);
    const row=document.createElement(heading || bold ? "strong" : "span");
    if(fenced) row.className="source-code";
    const value=heading ? heading[2] : bold ? bold[2] : line;
    const pattern=/\[([^\]\n]+)\]\((?:<([^>\n]+)>|([^\s)]+))\)/g;
    let cursor=0;
    for(const match of value.matchAll(pattern)) {
      row.append(document.createTextNode(value.slice(cursor,match.index)));
      const target=match[2] || match[3]; let safe=false;
      try {const url=new URL(target);safe=["http:","https:","mailto:"].includes(url.protocol) && !url.username && !url.password;} catch(_){/* Literal unsupported target. */}
      if(safe){const a=document.createElement("a");a.textContent=match[1];a.href=target;a.target="_blank";a.rel="noopener noreferrer";row.append(a);}
      else row.append(document.createTextNode(match[0]));
      cursor=match.index+match[0].length;
    }
    row.append(document.createTextNode(value.slice(cursor)));
    root.append(row,document.createTextNode("\n"));
  }
  return root;
};
