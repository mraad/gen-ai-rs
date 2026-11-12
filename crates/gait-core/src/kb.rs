//! A small skill catalog. Instruction bodies and resources are fetched only when needed.
use crate::llm::Http;
use crate::{Error, Result};
use serde::Deserialize;
use std::collections::BTreeMap;

#[derive(Clone, Debug, Deserialize)]
pub struct Skill {
    pub name: String,
    pub description: String,
    pub hash: String,
    #[serde(default)]
    pub resources: Vec<String>,
}

#[derive(Deserialize)]
pub struct Kb {
    pub instructions: String,
    #[serde(default)]
    pub skills: BTreeMap<String, Skill>,
    #[serde(skip)]
    pub base_url: String,
}

impl Kb {
    pub async fn load(base_url: &str, http: &dyn Http) -> Result<Self> {
        let base_url = base_url.trim_end_matches('/');
        let mut kb: Self =
            serde_json::from_slice(&fetch(http, &format!("{base_url}/kb.json")).await?)?;
        for (name, skill) in &kb.skills {
            if name != &skill.name
                || !crate::mcp::valid_name(name)
                || name.contains("..")
                || skill.description.trim().is_empty()
                || skill.hash.is_empty()
                || skill.resources.iter().any(|p| {
                    p.is_empty() || p.starts_with('/') || p.contains('\\') || p.contains("..")
                })
            {
                return Err(Error::Kb(format!("invalid skill '{name}'")));
            }
        }
        kb.base_url = base_url.to_owned();
        Ok(kb)
    }
}

pub(crate) async fn fetch(http: &dyn Http, url: &str) -> Result<Vec<u8>> {
    let response = http.get(url).await?;
    if !response.is_success() {
        return Err(Error::Http {
            status: response.status,
            body: format!("GET {url}"),
        });
    }
    Ok(response.body)
}
