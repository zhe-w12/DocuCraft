const WORKSPACE_STYLES = `
html,body{min-height:100%;}
body{padding:0 0 36px;background:#eff1f7;}
.toolbar{width:100%;max-width:none;margin:0;border-radius:0;border-left:0;border-right:0;padding:11px 18px;}
.page,.original-page,.coordinate-page{margin-top:22px;margin-bottom:22px;}
`;

const SOURCE_LAYOUT_STYLES = `
.toolbar button.view-button.is-current:disabled{opacity:1;cursor:default;color:#514be1;background:#f2f1ff;border-color:#d9d7fa;}
`;

export function prepareEditorWorkspaceHtml(html) {
  const source = String(html || "");
  return source.replace(/<\/style>/i, `${WORKSPACE_STYLES}</style>`);
}

export function prepareImageEditorWorkspaceHtml(html) {
  let prepared = prepareEditorWorkspaceHtml(html);
  const hasEditableSourceLayout = /id=(?:["']coordinatePage["']|coordinatePage(?=[\s>]))/i.test(prepared)
    && /data-(?:coordinate-cell|positioned-word)=(?:["']1["']|1(?=[\s>]))/i.test(prepared);
  if (!hasEditableSourceLayout) return prepared;

  const sourceLayoutEditor = prepared.replace(
    /<button\b(?=[^>]*\bid=(?:["']coordinateToggle["']|coordinateToggle(?=[\s>])))[^>]*>[\s\S]*?<\/button>/i,
    "<button class='view-button is-current' type='button' disabled aria-current='page'>版式编辑</button>",
  );
  if (sourceLayoutEditor === prepared) return prepared;
  return sourceLayoutEditor.replace(/<\/style>/i, `${SOURCE_LAYOUT_STYLES}</style>`);
}
