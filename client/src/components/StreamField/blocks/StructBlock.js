/* global $ */

import { escapeHtml as h } from '../../../utils/text';
import { hasOwn } from '../../../utils/hasOwn';
import {
  addErrorMessages,
  removeErrorMessages,
} from '../../../includes/streamFieldErrors';
import { CollapsiblePanel } from './CollapsiblePanel';
import { initCollapsiblePanel } from '../../../includes/panels';
import { setAttrs } from '../../../utils/attrs';

export class StructBlock {
  constructor(blockDef, placeholder, prefix, initialState, initialError) {
    const state = initialState || {};
    this.blockDef = blockDef;
    this.type = blockDef.name;

    this.childBlocks = {};

    let container = '';
    if (this.blockDef.collapsible) {
      container = new CollapsiblePanel({
        panelId: prefix + '-section',
        headingId: prefix + '-heading',
        contentId: prefix + '-content',
        blockTypeIcon: h(blockDef.meta.icon),
        blockTypeLabel: h(blockDef.meta.label),
        collapsed: blockDef.meta.collapsed,
      }).render().outerHTML;
    }

    if (blockDef.meta.formTemplate) {
      let dom = $(container);
      $(placeholder).replaceWith(dom);

      if (this.blockDef.collapsible) {
        dom = this.#initializeCollapsiblePanel(dom, prefix);
      }

      const html = blockDef.meta.formTemplate.replace(/__PREFIX__/g, prefix);
      dom.append(html);

      const blockErrors = initialError?.blockErrors || {};
      this.blockDef.childBlockDefs.forEach((childBlockDef) => {
        const childBlockElement = dom
          .find('[data-structblock-child="' + childBlockDef.name + '"]')
          .get(0);
        const childBlock = childBlockDef.render(
          childBlockElement,
          prefix + '-' + childBlockDef.name,
          state[childBlockDef.name],
          blockErrors[childBlockDef.name],
        );
        this.childBlocks[childBlockDef.name] = childBlock;
      });
      this.container = dom;
    } else {
      let dom = $(`
        <div class="${h(this.blockDef.meta.classname || '')}">
        </div>
      `);
      dom.append(container);
      $(placeholder).replaceWith(dom);

      if (this.blockDef.collapsible) {
        dom = this.#initializeCollapsiblePanel(dom, prefix);
      }

      if (this.blockDef.meta.helpText) {
        // help text is left unescaped as per Django conventions
        dom.append(`
          <div class="c-sf-help">
            <div class="help">
              ${this.blockDef.meta.helpText}
            </div>
          </div>
        `);
      }

      this.blockDef.childBlockDefs.forEach((childBlockDef) => {
        const isStructBlock =
          // Cannot use `instanceof StructBlockDefinition` here as it is defined
          // later in this file. Compare our own blockDef constructor instead.
          childBlockDef instanceof this.blockDef.constructor;

        // Struct blocks are collapsible and thus have their own header,
        // so only add the label if this is not a struct block.
        let label = '';
        if (!isStructBlock) {
          label = `<label class="w-field__label">${h(childBlockDef.meta.label)}${
            childBlockDef.meta.required
              ? '<span class="w-required-mark">*</span>'
              : ''
          }</label>`;
        }

        const childDom = $(`
        <div data-contentpath="${childBlockDef.name}">
          ${label}
            <div data-streamfield-block></div>
          </div>
        `);
        dom.append(childDom);
        const childBlockElement = childDom
          .find('[data-streamfield-block]')
          .get(0);
        const labelElement = childDom.find('label').get(0);
        const blockErrors = initialError?.blockErrors || {};
        const childBlock = childBlockDef.render(
          childBlockElement,
          prefix + '-' + childBlockDef.name,
          state[childBlockDef.name],
          blockErrors[childBlockDef.name],
          new Map(),
        );

        this.childBlocks[childBlockDef.name] = childBlock;
        if (childBlock.idForLabel) {
          labelElement.setAttribute('for', childBlock.idForLabel);
        }
      });
      this.container = dom;
    }

    // Set in initialisation regardless of block state for screen reader users.
    if (this.blockDef.collapsible) {
      this.setTextLabel();
    }

    setAttrs(this.container[0], this.blockDef.meta.attrs || {});
  }

  #initializeCollapsiblePanel(dom, prefix) {
    const collapsibleToggle = dom.find('[data-panel-toggle]')[0];
    const collapsibleTitle = dom.find('[data-panel-heading-text]')[0];
    initCollapsiblePanel(collapsibleToggle);
    this.setTextLabel = () => {
      const label = this.getTextLabel({ maxLength: 50 });
      collapsibleTitle.textContent = label || '';
    };
    collapsibleToggle.addEventListener(
      'wagtail:panel-toggle',
      this.setTextLabel,
    );
    return dom.find(`#${prefix}-content`);
  }

  setState(state) {
    // eslint-disable-next-line guard-for-in
    for (const name in state) {
      this.childBlocks[name].setState(state[name]);
    }
  }

  setError(error) {
    if (!error) return;

    // Non block errors
    const container = this.container[0];
    removeErrorMessages(container);
    if (error.messages) {
      addErrorMessages(container, error.messages);
    }

    if (error.blockErrors) {
      for (const blockName in error.blockErrors) {
        if (hasOwn(error.blockErrors, blockName)) {
          this.childBlocks[blockName].setError(error.blockErrors[blockName]);
        }
      }
    }
  }

  getState() {
    const state = {};
    // eslint-disable-next-line guard-for-in
    for (const name in this.childBlocks) {
      state[name] = this.childBlocks[name].getState();
    }
    return state;
  }

  getDuplicatedState() {
    const state = {};
    // eslint-disable-next-line guard-for-in
    for (const name in this.childBlocks) {
      const block = this.childBlocks[name];
      state[name] =
        block.getDuplicatedState === undefined
          ? block.getState()
          : block.getDuplicatedState();
    }
    return state;
  }

  getValue() {
    const value = {};
    // eslint-disable-next-line guard-for-in
    for (const name in this.childBlocks) {
      value[name] = this.childBlocks[name].getValue();
    }
    return value;
  }

  getTextLabel(opts) {
    if (this.blockDef.meta.labelFormat) {
      /* use labelFormat - regexp replace any field references like '{first_name}'
      with the text label of that sub-block */
      return this.blockDef.meta.labelFormat.replace(
        /\{(\w+)\}/g,
        (tag, blockName) => {
          const block = this.childBlocks[blockName];
          if (block && block.getTextLabel) {
            /* to be strictly correct, we should be adjusting opts.maxLength to account for the overheads
          in the format string, and dividing the remainder across all the placeholders in the string,
          rather than just passing opts on to the child. But that would get complicated, and this is
          better than nothing... */
            return block.getTextLabel(opts);
          }
          return '';
        },
      );
    }

    /* if no labelFormat specified, just try each child block in turn until we find one that provides a label */
    for (const childDef of this.blockDef.childBlockDefs) {
      const child = this.childBlocks[childDef.name];
      if (child.getTextLabel) {
        const val = child.getTextLabel(opts);
        if (val) return val;
      }
    }
    // no usable label found
    return null;
  }

  focus(opts) {
    if (this.blockDef.childBlockDefs.length) {
      const firstChildName = this.blockDef.childBlockDefs[0].name;
      this.childBlocks[firstChildName].focus(opts);
    }
  }
}

export class StructBlockDefinition {
  constructor(name, childBlockDefs, meta) {
    this.name = name;
    this.childBlockDefs = childBlockDefs;
    this.meta = meta;

    // Always collapsible by default, but can be overridden e.g. when used in a
    // StreamBlock or ListBlock, in which case the collapsible behavior is
    // controlled by the parent block.
    this.collapsible = true;
  }

  render(placeholder, prefix, initialState, initialError) {
    return new StructBlock(
      this,
      placeholder,
      prefix,
      initialState,
      initialError,
    );
  }
}
