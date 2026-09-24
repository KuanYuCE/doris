# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

variable "job_name" {
  type        = string
  default     = "doris"
  description = "Stable job ID; credentials live at nomad/jobs/<job_name>."
}

variable "namespace" {
  type        = string
  default     = "default"
  description = "Nomad namespace containing the job and its credentials."
}

variable "datacenters" {
  type        = list(string)
  default     = ["dc1"]
  description = "Eligible Nomad datacenters."
}

variable "bootstrap_fe" {
  type        = string
  description = "IP of the only FE allowed to consume a fresh-cluster bootstrap permit."
}

variable "discovery_fe_ips" {
  type        = list(string)
  description = "Stable seed FE IPs. Keep unchanged when adding nodes so existing groups are not redeployed. Seeds return the current master, which need not itself be a seed."
}

variable "fe_nodes" {
  type = list(object({
    ip       = string
    hostname = string
    volume   = string
    image    = string
  }))
  description = "Stable FE identities. hostname is the Nomad client node name; volume is its host volume."
}

variable "be_nodes" {
  type = list(object({
    ip       = string
    hostname = string
    volume   = string
    image    = string
  }))
  description = "Stable BE identities, with a persistent host volume and per-node image."
}

variable "discovery_timeout" {
  type        = number
  default     = 120
  description = "Seconds to discover/register with an elected master before failing or using the bootstrap permit."
}

variable "fe_cpu" {
  type        = number
  default     = 2000
  description = "FE CPU reservation in MHz."
}

variable "fe_memory" {
  type        = number
  default     = 16384
  description = "FE memory limit in MiB; must accommodate the image's JVM heap and native memory."
}

variable "be_cpu" {
  type        = number
  default     = 4000
  description = "BE CPU reservation in MHz."
}

variable "be_memory" {
  type        = number
  default     = 16384
  description = "BE memory limit in MiB."
}
