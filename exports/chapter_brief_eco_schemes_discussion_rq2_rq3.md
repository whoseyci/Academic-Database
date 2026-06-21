# Chapter Brief: Discussion evidence brief for eco-scheme participation drivers and design improvements

Purpose: Provide an LLM or human writer with source-backed claims for discussing behavioural determinants of participation and design modifications for CAP eco-schemes. This profile is thesis-specific, but illustrates the generic chapter-brief mechanism.

## Writing contract

- Use only claims in this brief unless you explicitly retrieve more evidence.
- Every substantive sentence in the drafted chapter should map to at least one claim_id.
- Candidate or page-check claims are not publication-safe until reviewed; flag them for audit.
- Call `python rh2.py context CLAIM_ID --window 600` before using a claim as a central argument.
- Do not generalize beyond the source scope: European AECM evidence is not automatically Andalusian olive-grove evidence.
- Preserve uncertainties, limitations, and design trade-offs, especially additionality/self-selection and undermeasured contract/social variables.

## Coverage

- Claims: 22
- Sources: {'Canessa_2024': 22}
- Statuses: {'needs_page_check': 22}

## Warnings

- No verified claims in this chapter brief; use candidate claims for drafting only after review.

## Behavioural and institutional drivers of participation

Writing goal: Explain why participation is not only financial: attitudes, trust, experience, information and social context matter.

Query: `trust policy stability social norms previous participation information pro-environmental attitudes adoption AECM farmers`

- **CLM-Canessa_2024-0005** [0.5385] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · theoretical claim · `paraphrase`
  - Claim: The theory of planned behaviour is explicitly relevant to AECM adoption because it links intentions and behaviour to attitudes, subjective norms, and perceived behavioural control.
  - Evidence: the theory of planned behaviour considers how individual intentions and behaviours are being influenced by personal attitudes, perceived social pressure (i.e., subjective norms), and perceived behavioural control
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0005 --window 500`

- **CLM-Canessa_2024-0021** [0.5385] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · empirical finding · `paraphrase`
  - Claim: Previous participation in AECM is a strong predictor of later participation, suggesting that experience can reduce information asymmetry and improve policy trust.
  - Evidence: Participation in previous AECM or other types of subsidised programs was also found to increase the likelihood of participation in most of the observed models (68% out of 28 models), illustrating the importance of experience in reducing information asymmetry and improving trust in public policies
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0021 --window 500`

- **CLM-Canessa_2024-0022** [0.5385] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · empirical finding · `paraphrase`
  - Claim: Neighbouring farmers’ opinions or participation, when significant, consistently showed a positive effect on AECM adoption.
  - Evidence: For variables capturing the role of neighbouring or other farmers’ opinions on AECM, whenever we found a significant adoption effect, it was positive
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0022 --window 500`

- **CLM-Canessa_2024-0020** [0.4615] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · empirical finding · `paraphrase`
  - Claim: Trust and perceived policy stability were positively associated with AECM uptake in most significant cases, though they were rarely studied.
  - Evidence: most of the significant results (57%) suggest that the perceived stability of policy instruments and favourable attitudes towards institutions are positively correlated with AECM uptake
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0020 --window 500`

- **CLM-Canessa_2024-0001** [0.3846] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · theoretical claim · `paraphrase`
  - Claim: Because AECM are voluntary, farmer participation is the first indicator of programme success and eventual environmental effectiveness.
  - Evidence: Due to the voluntary nature of agri-environmental-climate measures (AECM), adequate and effective participation of farmers in these initiatives is the first key indicator of their success and, eventually, their effectiveness.
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0001 --window 500`

- **CLM-Canessa_2024-0003** [0.3846] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · empirical finding · `paraphrase`
  - Claim: Action-based AECM are predominant partly because they are easier to implement, monitor, and accept, but they still show mixed participation and environmental-effectiveness records.
  - Evidence: WTO alignment, along with the ease of implementation, monitoring, and general acceptability by farmers, collectively favour a predominant farmer preference of action-over result-based AECM
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0003 --window 500`

- **CLM-Canessa_2024-0004** [0.3846] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · theoretical claim · `paraphrase`
  - Claim: AECM can fail either because payments are too low to induce adoption or because they attract baseline-complying farmers with low or zero additionality.
  - Evidence: Payments offered may be too small to compensate the cost incurred by the farmer
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0004 --window 500`

- **CLM-Canessa_2024-0008** [0.3846] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · theoretical claim · `paraphrase`
  - Claim: Environmental attitudes influence whether farmers perceive an AECM as relevant and acceptable.
  - Evidence: Attitude variables, describing the importance that farmers place on the environment, also play an important role in affecting the perceived relevance of the measure
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0008 --window 500`


## Contract design, flexibility and transaction costs

Writing goal: Discuss how payment design, bureaucratic simplification, implementation ease and flexibility shape participation incentives.

Query: `contract design flexibility bureaucratic simplification transaction costs payment compensation opportunity costs participation`

- **CLM-Canessa_2024-0025** [0.5455] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · empirical finding · `paraphrase`
  - Claim: Bureaucratic simplification, fairness/flexibility, and higher compensation levels were positively linked to AECM uptake in the limited ex-post studies that measured contract features.
  - Evidence: some studies reported a positive impact on uptake from greater ease of implementation/ bureaucratic simplification, increased fairness and flexibility, and higher compensation levels
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0025 --window 500`

- **CLM-Canessa_2024-0013** [0.4545] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · policy implication · `paraphrase`
  - Claim: Farmers generally prefer AECM contracts that are simple, understandable and flexible, while flat payments can deter farmers with high compliance costs.
  - Evidence: farmers receive a flat payment that is not customized to the heterogeneity of compliance costs. This can deter those with high compliance costs from participating.
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0013 --window 500`

- **CLM-Canessa_2024-0032** [0.4545] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · policy implication · `paraphrase`
  - Claim: For farmers with higher opportunity costs, policy can either increase incentives through differentiated mechanisms or invest in nudging and signalling strategies.
  - Evidence: To encourage the participation of farmers with higher opportunity costs, AECM implementers can either increase incentives through tailored methods like auctions or differentiated payments ... or invest in nudging and signalling strategies
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0032 --window 500`

- **CLM-Canessa_2024-0011** [0.3636] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · theoretical claim · `paraphrase`
  - Claim: AECM participation costs include equipment, knowledge, working time, productivity losses, and reduced management flexibility.
  - Evidence: These include, for instance, the necessity of acquiring new inputs or specific equipment
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0011 --window 500`

- **CLM-Canessa_2024-0031** [0.3636] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · empirical finding · `paraphrase`
  - Claim: Social factors, cognitive factors and contract design deserve more attention because variables in these domains often show positive significant adoption effects when measured.
  - Evidence: variables explaining the role of lower transaction costs, social contexts, and satisfactory contract design frequently appear to be significant
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0031 --window 500`

- **CLM-Canessa_2024-0003** [0.2727] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · empirical finding · `paraphrase`
  - Claim: Action-based AECM are predominant partly because they are easier to implement, monitor, and accept, but they still show mixed participation and environmental-effectiveness records.
  - Evidence: WTO alignment, along with the ease of implementation, monitoring, and general acceptability by farmers, collectively favour a predominant farmer preference of action-over result-based AECM
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0003 --window 500`

- **CLM-Canessa_2024-0017** [0.2727] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · empirical finding · `paraphrase`
  - Claim: Lower household/farm income can make AECM participation more attractive because payments provide complementary income where opportunity costs are lower.
  - Evidence: 58% a significantly negative correlation. The latter argue that lower household and farm-related income can make participation in AECM more attractive to farmers, as income support enabled by lower opportunity costs
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0017 --window 500`

- **CLM-Canessa_2024-0018** [0.2727] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · empirical finding · `paraphrase`
  - Claim: Location in less-favoured areas is frequently associated with higher AECM participation, likely because lower productivity and income reduce opportunity costs and increase the value of complementary income support.
  - Evidence: Lower productivity often coincides with lower income and reduced opportunity costs, thus with the need for complementary farm-income support
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0018 --window 500`


## Participation versus additionality trade-offs

Writing goal: Show why high participation alone is insufficient if schemes mostly attract baseline-compliant farmers.

Query: `additionality self-selection baseline complying farmers alignment participation effectiveness action based AECM`

- **CLM-Canessa_2024-0003** [0.5833] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · empirical finding · `paraphrase`
  - Claim: Action-based AECM are predominant partly because they are easier to implement, monitor, and accept, but they still show mixed participation and environmental-effectiveness records.
  - Evidence: WTO alignment, along with the ease of implementation, monitoring, and general acceptability by farmers, collectively favour a predominant farmer preference of action-over result-based AECM
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0003 --window 500`

- **CLM-Canessa_2024-0004** [0.5833] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · theoretical claim · `paraphrase`
  - Claim: AECM can fail either because payments are too low to induce adoption or because they attract baseline-complying farmers with low or zero additionality.
  - Evidence: Payments offered may be too small to compensate the cost incurred by the farmer
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0004 --window 500`

- **CLM-Canessa_2024-0030** [0.5] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · policy implication · `paraphrase`
  - Claim: Alignment between AECM and farmer attitudes/operations often matters for adoption, but excessive alignment risks attracting farmers who would have complied anyway.
  - Evidence: increasing alignment between the AECM and farmer objectives excessively could aggravate selection biases undermining the AECM’s capacity to achieve environmental objectives
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0030 --window 500`

- **CLM-Canessa_2024-0001** [0.3333] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · theoretical claim · `paraphrase`
  - Claim: Because AECM are voluntary, farmer participation is the first indicator of programme success and eventual environmental effectiveness.
  - Evidence: Due to the voluntary nature of agri-environmental-climate measures (AECM), adequate and effective participation of farmers in these initiatives is the first key indicator of their success and, eventually, their effectiveness.
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0001 --window 500`

- **CLM-Canessa_2024-0002** [0.25] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · theoretical claim · `paraphrase`
  - Claim: Effective participation requires not only many farmers but the right types of farmers, because environmental additionality depends on services that would not have been provided without the programme.
  - Evidence: Effective participation refers to a certain number and well-targeted types of farmers implementing sustainable practices, which eventually provide additional environmental services that, counterfactually, would not have been supplied without the programme.
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0002 --window 500`

- **CLM-Canessa_2024-0006** [0.25] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · theoretical claim · `paraphrase`
  - Claim: Canessa et al. organise AECM participation determinants into four decision-process categories: alignment, opportunity, engagement, and contracting.
  - Evidence: we expanded upon Whitten et al. (2013) framework by identifying eight major factors and grouping them into four categories: alignment, opportunity, engagement, and contracting.
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0006 --window 500`


## Evidence gaps and methodological implications

Writing goal: Identify variables that are undermeasured and justify why the thesis survey focus is valuable.

Query: `undermeasured social context contract design engagement transaction cost standardized behavioural indicators research gaps`

- **CLM-Canessa_2024-0033** [0.3846] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · methodological claim · `paraphrase`
  - Claim: Future adoption research should use more standardised indicators for behavioural factors and transaction costs.
  - Evidence: researchers should work towards developing more standardized indicators for behavioural factors (e.g., awareness, environmental attitudes, openness to innovation, risk preferences) and transaction costs
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0033 --window 500`

- **CLM-Canessa_2024-0015** [0.2308] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · methodological claim · `paraphrase`
  - Claim: The literature often over-measures alignment and opportunity variables while under-measuring engagement, contracting and transaction-cost variables.
  - Evidence: variables explaining engagement or contracting are less frequently observed. Yet, despite being occasionally observed, engagement and contracting variables hold a high share of significance, as do transaction cost variables
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0015 --window 500`

- **CLM-Canessa_2024-0029** [0.2308] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · policy implication · `paraphrase`
  - Claim: Some participation determinants are not easily changed by regulators, while information, organisational engagement and scheme design improvements are more actionable in the short to medium term.
  - Evidence: aspects affecting participation that can be addressed by AECM implementers in the medium-short term (e.g., information, engagement with organizations, and scheme design improvements)
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0029 --window 500`

- **CLM-Canessa_2024-0034** [0.2308] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · policy implication · `paraphrase`
  - Claim: Engagement and contract design are high-yield intervention points because information, interpersonal communication and satisfaction with design were frequently significant and positively linked to adoption.
  - Evidence: Variables reflecting the role of information and interpersonal communication, as well as farmers’ satisfaction with the AECM design, were more frequently significant and positively linked to adoption
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0034 --window 500`

- **CLM-Canessa_2024-0005** [0.1538] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · theoretical claim · `paraphrase`
  - Claim: The theory of planned behaviour is explicitly relevant to AECM adoption because it links intentions and behaviour to attitudes, subjective norms, and perceived behavioural control.
  - Evidence: the theory of planned behaviour considers how individual intentions and behaviours are being influenced by personal attitudes, perceived social pressure (i.e., subjective norms), and perceived behavioural control
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0005 --window 500`

- **CLM-Canessa_2024-0013** [0.1538] grade:C · Canessa; Ait-Sidhoum; Wunder; Sauer (2024) p.? · needs_page_check · policy implication · `paraphrase`
  - Claim: Farmers generally prefer AECM contracts that are simple, understandable and flexible, while flat payments can deter farmers with high compliance costs.
  - Evidence: farmers receive a flat payment that is not customized to the heterogeneity of compliance costs. This can deter those with high compliance costs from participating.
  - Deep dive: `python rh2.py context CLM-Canessa_2024-0013 --window 500`
