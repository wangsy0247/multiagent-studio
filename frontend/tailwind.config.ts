import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: ["class"],
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        // Signature Hermès orange — the only vivid accent in the system
        hermes: {
          50: "hsl(30 100% 97%)",
          100: "hsl(29 95% 92%)",
          200: "hsl(28 90% 83%)",
          300: "hsl(27 88% 72%)",
          400: "hsl(25 88% 61%)",
          500: "hsl(24 89% 54%)",
          600: "hsl(22 85% 48%)",
          700: "hsl(22 84% 40%)",
          800: "hsl(21 78% 34%)",
          900: "hsl(21 70% 28%)",
          950: "hsl(20 72% 18%)",
        },
        // slate remapped from cool blue-grey to warm greige so the ~999
        // existing slate-* classes turn warm without touching every file
        slate: {
          50: "hsl(36 33% 97%)",
          100: "hsl(35 25% 94%)",
          200: "hsl(30 18% 89%)",
          300: "hsl(30 14% 82%)",
          400: "hsl(30 9% 63%)",
          500: "hsl(28 8% 46%)",
          600: "hsl(28 10% 34%)",
          700: "hsl(26 13% 26%)",
          800: "hsl(25 18% 17%)",
          900: "hsl(25 30% 12%)",
          950: "hsl(24 35% 8%)",
        },
      },
      fontFamily: {
        sans: [
          "var(--font-sans)",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "PingFang SC",
          "Microsoft YaHei",
          "sans-serif",
        ],
        display: [
          "var(--font-display)",
          "Playfair Display",
          "Georgia",
          "Songti SC",
          "Noto Serif SC",
          "SimSun",
          "serif",
        ],
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      boxShadow: {
        // luxury surfaces stay flat; shadows only appear on hover/emphasis
        xs: "0 1px 2px 0 rgba(40, 28, 18, 0.03)",
        sm: "0 1px 3px 0 rgba(40, 28, 18, 0.05), 0 1px 2px -1px rgba(40, 28, 18, 0.05)",
        md: "0 4px 8px -1px rgba(40, 28, 18, 0.06), 0 2px 4px -2px rgba(40, 28, 18, 0.04)",
        lg: "0 10px 16px -3px rgba(40, 28, 18, 0.07), 0 4px 6px -4px rgba(40, 28, 18, 0.04)",
      },
      keyframes: {
        "pulse-glow": {
          "0%, 100%": { boxShadow: "0 0 8px hsl(24 89% 54% / 0.4)" },
          "50%": { boxShadow: "0 0 20px hsl(24 89% 54% / 0.65)" },
        },
        "fade-in-up": {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "fade-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
        "scale-in": {
          from: { opacity: "0", transform: "scale(0.95)" },
          to: { opacity: "1", transform: "scale(1)" },
        },
        "gradient-shift": {
          "0%": { backgroundPosition: "0% 50%" },
          "50%": { backgroundPosition: "100% 50%" },
          "100%": { backgroundPosition: "0% 50%" },
        },
      },
      animation: {
        "pulse-glow": "pulse-glow 2s ease-in-out infinite",
        "fade-in-up": "fade-in-up 0.35s ease-out both",
        "fade-in": "fade-in 0.3s ease-out both",
        "scale-in": "scale-in 0.25s ease-out both",
        "gradient": "gradient-shift 8s ease infinite",
      },
    },
  },
  plugins: [require("tailwindcss-animate"), require("@tailwindcss/typography")],
};

export default config;
