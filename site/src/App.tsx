import { Benchmarks } from "./components/Benchmarks";
import { Explorer } from "./components/Explorer";
import { Footer } from "./components/Footer";
import { Header } from "./components/Header";
import { Hero } from "./components/Hero";
import { HowItWorks } from "./components/HowItWorks";
import { MemoryDemo } from "./components/MemoryDemo";

export function App() {
  return (
    <>
      <a className="skip" href="#main">
        Skip to content
      </a>
      <Header />
      <main id="main">
        <Hero />
        <HowItWorks />
        <Explorer />
        <Benchmarks />
        <MemoryDemo />
      </main>
      <Footer />
    </>
  );
}
