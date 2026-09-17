/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./src/renderer/**/*.{html,js,ts,tsx}'],
  theme: {
    extend: {
      colors: {
        base: 'var(--color-bg-base)',
        sidebar: 'var(--color-bg-sidebar)',
        elevated: 'var(--color-bg-elevated)',
        'surface-hover': 'var(--color-bg-surface-hover)',
        edge: 'var(--color-border)',
        'edge-strong': 'var(--color-border-strong)',
        primary: 'var(--color-text-primary)',
        secondary: 'var(--color-text-secondary)',
        accent: 'var(--color-accent)',
        'accent-hover': 'var(--color-accent-hover)',
        onaccent: 'var(--color-on-accent)',
        ok: 'var(--color-ok)',
        warn: 'var(--color-warn)',
        danger: 'var(--color-danger)',
        ring: 'var(--color-ring)',
        backdrop: 'var(--color-backdrop)'
      },
      borderRadius: {
        card: 'var(--radius-card)',
        control: 'var(--radius-control)',
        pill: 'var(--radius-pill)'
      },
      fontFamily: {
        ui: 'var(--font-ui)'
      },
      fontSize: {
        xs: 'var(--text-xs)',
        sm: 'var(--text-sm)',
        base: 'var(--text-base)',
        lg: 'var(--text-lg)',
        xl: 'var(--text-xl)'
      },
      transitionDuration: {
        fast: 'var(--motion-fast)',
        normal: 'var(--motion-normal)',
        slow: 'var(--motion-slow)'
      },
      transitionTimingFunction: {
        app: 'var(--ease-app)'
      }
    }
  },
  plugins: []
};
