import AnnouncementBar from "../components/AnnouncementBar.jsx";
import BuiltOn from "../components/BuiltOn.jsx";
import FinalCta from "../components/FinalCta.jsx";
import Hero from "../components/Hero.jsx";
import HowItWorks from "../components/HowItWorks.jsx";
import Pricing from "../components/Pricing.jsx";
import SiteFooter from "../components/SiteFooter.jsx";
import SiteHeader from "../components/SiteHeader.jsx";
import SkipLink from "../components/SkipLink.jsx";
import TrustSection from "../components/TrustSection.jsx";
import WhatWeDo from "../components/WhatWeDo.jsx";
import WhatYouGet from "../components/WhatYouGet.jsx";
import WhyWeBuiltIt from "../components/WhyWeBuiltIt.jsx";

export default function HomePage() {
  return (
    <>
      <SkipLink />
      <AnnouncementBar />
      <SiteHeader />
      <main id="main">
        <Hero />
        <WhatWeDo />
        <WhatYouGet />
        <WhyWeBuiltIt />
        <TrustSection />
        <HowItWorks />
        <Pricing />
        <BuiltOn />
        <FinalCta />
      </main>
      <SiteFooter />
    </>
  );
}
