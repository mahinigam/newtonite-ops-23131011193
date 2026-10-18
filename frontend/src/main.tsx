/**
 * Entry point.
 *
 * The query client is created once, outside React, so it survives a re-render and so its
 * error handling (a `401` ends the session) is installed before any request is made.
 */

import { StrictMode, useEffect, useRef } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'

import { App } from './App'
import { createQueryClient } from './query/client'
import './styles.css'

const queryClient = createQueryClient()

/**
 * Decorative-only artwork shared by every screen. It does not participate in application
 * state, routing, or input handling: the canvas is pointer-events:none and all SVG shapes
 * are hidden from assistive technology.
 */
function CrayonEffects() {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const finePointer = window.matchMedia('(pointer: fine)').matches
    if (reducedMotion || !finePointer) return

    const canvas = canvasRef.current
    if (!canvas) return
    const context = canvas.getContext('2d')
    if (!context) return
    const activeCanvas: HTMLCanvasElement = canvas
    const drawingContext: CanvasRenderingContext2D = context

    let frame = 0
    let lastMove = 0
    const colors = ['#294B3A', '#3F4F80', '#174A78', '#5F9B7E']

    function resize() {
      const scale = window.devicePixelRatio || 1
      activeCanvas.width = Math.round(window.innerWidth * scale)
      activeCanvas.height = Math.round(window.innerHeight * scale)
      drawingContext.setTransform(scale, 0, 0, scale, 0, 0)
    }

    function fade(now: number) {
      drawingContext.save()
      drawingContext.globalCompositeOperation = 'destination-out'
      drawingContext.fillStyle = 'rgb(0 0 0 / 4.5%)'
      drawingContext.fillRect(0, 0, window.innerWidth, window.innerHeight)
      drawingContext.restore()

      if (now - lastMove < 5000) frame = window.requestAnimationFrame(fade)
      else {
        drawingContext.clearRect(0, 0, window.innerWidth, window.innerHeight)
        frame = 0
      }
    }

    function paint(event: PointerEvent) {
      lastMove = performance.now()
      for (let index = 0; index < 7; index += 1) {
        drawingContext.beginPath()
        drawingContext.fillStyle = colors[Math.floor(Math.random() * colors.length)] ?? '#294B3A'
        drawingContext.globalAlpha = 0.25 + Math.random() * 0.4
        drawingContext.arc(
          event.clientX + (Math.random() - 0.5) * 14,
          event.clientY + (Math.random() - 0.5) * 14,
          0.6 + Math.random() * 2.6,
          0,
          Math.PI * 2,
        )
        drawingContext.fill()
      }
      drawingContext.globalAlpha = 1
      if (!frame) frame = window.requestAnimationFrame(fade)
    }

    resize()
    window.addEventListener('resize', resize)
    window.addEventListener('pointermove', paint, { passive: true })
    return () => {
      window.cancelAnimationFrame(frame)
      window.removeEventListener('resize', resize)
      window.removeEventListener('pointermove', paint)
    }
  }, [])

  return (
    <div className="crayon-effects" aria-hidden="true">
      <svg width="0" height="0" style={{ position: 'absolute' }}>
        <filter id="crayon" x="-5%" y="-30%" width="110%" height="160%">
          <feTurbulence type="fractalNoise" baseFrequency=".04" numOctaves="3" seed="2" result="w" />
          <feDisplacementMap in="SourceGraphic" in2="w" scale="7" result="d" />
          <feTurbulence type="fractalNoise" baseFrequency=".75" numOctaves="2" seed="7" result="g" />
          <feColorMatrix
            in="g"
            type="matrix"
            values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 2.8 0 0 0 -.65"
            result="m"
          />
          <feComposite in="d" in2="m" operator="in" />
        </filter>
        <filter id="rough">
          <feTurbulence type="fractalNoise" baseFrequency=".035" numOctaves="3" seed="4" />
          <feDisplacementMap in="SourceGraphic" scale="14" />
        </filter>
      </svg>
      <div className="crayon-effects__blob crayon-effects__blob--green" />
      <div className="crayon-effects__blob crayon-effects__blob--blue" />
      <div className="crayon-effects__blocks">
        <span className="crayon-effects__block" />
        <span className="crayon-effects__block" />
        <span className="crayon-effects__block" />
      </div>
      <canvas ref={canvasRef} className="crayon-cursor" />
    </div>
  )
}

const container = document.getElementById('root')
if (!container) throw new Error('The #root element is missing from index.html')

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <CrayonEffects />
      <App />
    </QueryClientProvider>
  </StrictMode>,
)
