import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '../styles/tokens.css';
import './overlay.css';
import { OverlayApp } from './OverlayApp';

const container = document.getElementById('overlay-root');
if (!container) {
  throw new Error('overlay root missing');
}
createRoot(container).render(
  <StrictMode>
    <OverlayApp />
  </StrictMode>
);
