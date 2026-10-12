// Update presentation without resetting test state, charts, or operator edits.
(() => {
  let revision;
  async function refresh() {
    try {
      const response = await fetch('/branding/status', {cache:'no-store'});
      if (!response.ok) return;
      const brand = await response.json();
      if (revision === brand.revision) return;
      revision = brand.revision;
      const oldName = document.querySelector('[data-brand-name]')?.textContent;
      if (oldName && document.title.includes(oldName)) document.title = document.title.replace(oldName, brand.name);
      for (const [name,value] of [['name',brand.name],['subtitle',brand.subtitle],['mark',brand.mark]])
        document.querySelectorAll('[data-brand-'+name+']').forEach(el => el.textContent = value);
      const logo = document.querySelector('[data-brand-logo]');
      if (logo) { logo.hidden = !brand.logo; if (brand.logo) { logo.src=brand.logo+(brand.logo.includes('?')?'&':'?')+'brand='+revision; logo.alt=brand.name; } }
      document.querySelectorAll('[data-brand-mark]').forEach(el=>el.hidden=Boolean(brand.logo));
      let theme = document.querySelector('link[data-brand-theme]');
      if (brand.enabled) {
        if (!theme) { theme=document.createElement('link'); theme.rel='stylesheet'; theme.dataset.brandTheme=''; document.head.append(theme); }
        theme.href='/branding/theme.css?brand='+revision;
      } else theme?.remove();
      let icon = document.querySelector('link[data-brand-icon]');
      if (brand.favicon) {
        if (!icon) { icon=document.createElement('link'); icon.rel='icon'; icon.dataset.brandIcon=''; document.head.append(icon); }
        icon.href=brand.favicon;
      } else icon?.remove();
    } catch (_) { /* Keep the last applied presentation during reconnection. */ }
  }
  refresh(); setInterval(refresh, 10000);
})();
