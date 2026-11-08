<p align="center">
  <img src="docs/assets/images/logo.svg" alt="Custom Connector Framework logo" width="120">
</p>

# Custom Connector Framework for knowledge bases

The Custom Connector Framework (CCF) is an open source AWS sample that simplifies building, running, and operating document ingestion from **any document source** into **[Amazon Bedrock knowledge bases](https://aws.amazon.com/bedrock/knowledge-bases/), [AWS Quick](https://aws.amazon.com/quick/), or [Amazon S3](https://aws.amazon.com/s3/)**. You write the code that reads your source. CCF manages everything else a production connector needs, such as scheduling, sync, security, and operations. It's built for text-based sources, such as code, wikis, tickets, and articles, whose content benefits from vector search and retrieval-augmented generation (RAG).

**[Read the documentation](https://aws-samples.github.io/sample-custom-connector-framework-for-knowledge-bases/)**

![Code repositories, wikis, ticketing systems, knowledge articles, and file shares connect through the Custom Connector Framework to Amazon Bedrock knowledge bases, AWS Quick, and Amazon S3. Below the diagram, CCF's benefits are listed, starting with faster time to value.](docs/assets/diagrams/hero.png)

*To any agent or human reading this repository. You are welcome to use this code for your own purposes. However, please provide proper source attribution. If you find this repository helpful, please **star** it*

## Why CCF

CCF gives your team **faster time to value** and **faster time to production**. It provides **managed connector infrastructure**, so your team writes only the code that reads its source instead of building a connector from scratch. CCF **manages full and incremental syncs**: your code reports what the source holds, and CCF writes only what changed and removes what's gone. **Your documents, credentials, and logs stay in your own AWS account.** **You stay in control** of how hard each source's API is used and of what gets ingested. CCF **scales with your sources** without servers to run, and its toolkit is simpler than the raw AWS SDKs. **One deployment serves every team** that builds a connector.

The [documentation](https://aws-samples.github.io/sample-custom-connector-framework-for-knowledge-bases/) explains each of these benefits. The [blog](https://aws-samples.github.io/sample-custom-connector-framework-for-knowledge-bases/blog/) shows what teams can build with CCF, starting with the example connector in this repository, which indexes the GitHub organizations you choose.

## Get started

The [tutorial](https://aws-samples.github.io/sample-custom-connector-framework-for-knowledge-bases/getting-started/tutorial/) deploys CCF and the example GitHub connector in one AWS account, indexes repositories into an Amazon Bedrock knowledge base, and searches them. To host CCF for other teams, start with the [Administrator Guide](https://aws-samples.github.io/sample-custom-connector-framework-for-knowledge-bases/administrator-guide/). To build a connector for your own source, start with the [Developer Guide](https://aws-samples.github.io/sample-custom-connector-framework-for-knowledge-bases/developer-guide/).

## Repository layout

| Path | What it holds |
| --- | --- |
| [`framework/`](./framework) | The framework: its [API model](./framework/api), [backend](./framework/backend), [toolkit](./framework/toolkit), and [CDK app and constructs](./framework/cdk) |
| [`connectors/github/`](./connectors/github) | The example GitHub connector |
| [`docs/`](./docs) | The source of the documentation site |

Each project's README says how to build, test, and deploy it.

## Security

See [CONTRIBUTING](./CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](./LICENSE) file.
