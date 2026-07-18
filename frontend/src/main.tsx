import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/global.css'
import App from './App.tsx'
import { getDaemonToken } from './lib/token'

// Must run before the router mounts: a `?token=` query param has to be
// captured and stripped here, synchronously, or React Router's initial
// redirect (Navigate to /tasks) rewrites the URL first and the token is
// lost before DaemonProvider ever gets a chance to read it.
getDaemonToken()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
