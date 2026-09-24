# Third-Party Notices — Release Regression Pack

**Website:** https://xregcheck.com — **Support:** support@xregcheck.com

The Release Regression Pack is a hosted service: buyers receive results and
evidence only, and never receive or redistribute the underlying software.
First-party service code is licensed under the Apache License 2.0 — see
`LICENSE` at the root of this repository. This statement does not apply to
third-party components or override their licences and notices below.
The software below is executed solely by us, on our infrastructure, to
produce the deliverable. We publish this attribution page conservatively —
in the spirit of the licences below — even where a licence may not strictly
require hosted-use attribution. Full licence texts, and a component
inventory with SHA-256 hashes for every item, are maintained in our internal
manifest (`evidence/pilot/component-manifest-final.json`) and are available
on request via support@xregcheck.com.

---

## 1. KoSIT XML Validator 1.6.3

**Component:** `validator-1.6.3.jar` — **Licence:** Apache License 2.0

> KoSIT XML Validator
> Copyright 2017-2026 Koordinierungsstelle für IT-Standards (KoSIT)
>
> This product includes software developed by
> Koordinierungsstelle für IT-Standards (<https://xeinkauf.de/>).

## 2. KoSIT validator configuration for XRechnung

**Components:** configuration bundles v2026-01-31 (in service use) and
v2026-08-31 (held in pinned inventory); maintenance release-2025-07-10
bundle — **Licence:** Apache License 2.0

> KoSIT XML-Validator Configuration for XRechnung
> Copyright 2020 Koordinierungsstelle für IT-Standards
>
> This product includes software developed by
> Coordination Office for IT-Standards (http://www.xoev.de/).

## 3. XRechnung Schematron rules

**Components:** `xrechnung-schematron` v2.5.0, v2.6.0 and v2.4.0 (within
the release-2025-07-10 maintenance bundle) — **Licence:** Apache License
2.0 — Koordinierungsstelle für IT-Standards (KoSIT).

## 4. CEN EN 16931 validation artefacts

**Component:** CEN EN 16931 validation assets (CEN distribution 1.3.16;
maintenance bundles 1.3.14.2), including the EN 16931 Schematron-derived
rules as bundled by the CEN distribution — **Licence:** European Union
Public Licence (EUPL) v1.2.

> Licensed under European Union Public Licence (EUPL) version 1.2.

EUPL communication/source-offer duties for hosted use are under legal review;
this page is part of our conservative approach pending that review.

## 5. Saxon-HE 12.9

**Component:** `Saxon-HE-12.9.jar` — **Licence:** Mozilla Public License 2.0
(MPL 2.0)

> This product includes software developed at
> Saxonica (https://github.com/Saxonica/Saxon-HE).
> Licensed under MPL 2.0

## 6. Eclipse Temurin JRE (Java runtime)

**Component:** Eclipse Temurin OpenJDK runtime — **Licence:** GNU General
Public License version 2 with the Classpath Exception

> This product includes software developed at the Eclipse Foundation /
> Eclipse Adoptium (https://adoptium.net).
> Licensed under GPLv2 with Classpath Exception

## 7. Eclipse JAXB runtime stack

**Components:** `jaxb-core-4.0.8.jar`, `jaxb-runtime-4.0.8.jar`,
`txw2-4.0.8.jar`, `istack-commons-runtime-4.1.2.jar`,
`angus-activation-2.0.3.jar`, `jakarta.activation-api-2.1.4.jar`,
`jakarta.xml.bind-api-4.0.5.jar` — **Licence:** Eclipse Distribution
License v1.0 (EDL 1.0; BSD-style terms)

> This product includes software developed at
> Eclipse Foundation (https://github.com/eclipse-ee4j/jaxb-ri).
> Licensed under EDL 1.0

## 8. Apache Commons

**Components:** `commons-io-2.22.0.jar`, `commons-lang3-3.20.0.jar` —
**Licence:** Apache License 2.0

> This product includes software developed at
> The Apache Software Foundation (http://www.apache.org/).
> Licensed under Apache 2.0

## 9. Jansi 2.4.3

**Licence:** Apache License 2.0

> This product includes software developed at
> Fusesource (http://fusesource.github.io/jansi/).
> Licensed under Apache 2.0

## 10. picocli 4.7.7

**Licence:** Apache License 2.0

> This product includes software developed at
> Remko Popma (https://picocli.info/).
> Licensed under Apache 2.0

## 11. SLF4J 2.0.18 (`slf4j-api`, `slf4j-simple`)

**Licence:** MIT License

> This product includes software developed at
> QOS.ch (https://www.slf4j.org/).
> Licensed under MIT

## 12. XML Resolver 5.3.3 (`xmlresolver`, `xmlresolver-data`)

**Licence:** Apache License 2.0 — the data jar additionally bundles files
under the W3C Software License (`org/xmlresolver/notices/w3c-license.txt`).

> This product includes software developed by Norman Walsh / the xmlresolver
> project (https://github.com/xmlresolver/xmlresolver).
> Licensed under Apache 2.0

## 13. Peppol assets

**Component:** Peppol e-invoicing assets **as bundled by the KoSIT/CEN
distributions** (not obtained or redistributed by us directly from OpenPeppol)
— **Licence:** under review. We reproduce and redistribute no Peppol material;
peppol-asset files are executed only on our infrastructure as part of the
pinned bundles above.

## 14. UBL 2.1 schemas (Invoice / CreditNote)

**Component:** OASIS UBL 2.1 schema files **as bundled by the KoSIT/CEN
distributions** — **Licence:** under review (OASIS terms). Executed only on
our infrastructure; not distributed to buyers.

## 15. CII D16B schemas (Cross-Industry Invoice)

**Component:** UN/CEFACT Cross-Industry Invoice D16B schema files **as
bundled by the KoSIT/CEN distributions** — **Licence:** under review
(UN/CEFACT terms). Executed only on our infrastructure; not distributed to
buyers.

## 16. Python 3.13 (service runtime)

**Component:** CPython standard-library runtime used by the first-party
service code — **Licence:** Python Software Foundation License.

## 17. Trademarks

Product names used above (KoSIT, XRechnung, CEN, Saxon, Eclipse Temurin,
Peppol, UBL, UN/CEFACT, and others) are used for identification and
attribution only, remain the property of their respective owners, and imply
no endorsement of this service.

---

## TODO-LAWYER

1. Confirm the hosted-use (SaaS) notice obligations per component — in
   particular whether EUPL-licensed CEN artefacts require a
   communication/source-offer beyond this attribution page (L1).
2. Resolve licence provenance and required notices for Peppol assets, UBL
   2.1 schemas and CII D16B schemas as bundled in the KoSIT/CEN
   distributions (sections 13–15 are deliberately conservative).
3. Verify Temurin/JRE runtime distribution notices on the actual EU
   deployment image (the internal manifest flags base-image notices as
   unresolved) and add any required GPL/Classpath-Exception source-offer
   wording.
4. Confirm the all-rights-reserved statement covers only seller-owned code;
   update the internal manifest's first-party rights entry accordingly.
5. Confirm whether the full inventory status (currently recorded as
   INVENTORY_NOT_LEGAL_CLEARANCE) requires this page to be withheld or
   reworded until the L1 review completes.
6. Confirm no component forbids or conditions commercial hosted use
   (explicitly flagged in the review brief; assumed clear pending review).

Drafted with automated assistance and pending review by a qualified solicitor. Not legal advice.
